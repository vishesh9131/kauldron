# Copyright 2024 The kauldron Authors.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Evaluators."""

from __future__ import annotations

import collections.abc
import dataclasses
import functools
from typing import Any, Optional, TypeVar

from etils import epy
import jax
from kauldron import data
from kauldron import konfig
from kauldron import losses as losses_lib
from kauldron import metrics as metrics_lib
from kauldron import summaries as summaries_lib
from kauldron.train import config_lib
from kauldron.train import metric_writer
from kauldron.train import rngs_lib
from kauldron.train import train_step
from kauldron.utils import config_util
from kauldron.utils import jax_utils
from kauldron.utils import utils
from kauldron.utils.sharding_utils import sharding  # pylint: disable=g-importing-member
from kauldron.xm._src import run_strategies


_SelfT = TypeVar('_SelfT')

_DEFAULT_EVAL_NAME = 'eval'


class EvaluatorBase(config_util.BaseConfig, config_util.UpdateFromRootCfg):
  """Base class for inline evaluators.

  Evaluators should inherit from this class and implement the `evaluate` method.

  Attributes:
    name: Eval name (collection name for TensorBoard and Datatables)
    run: How/when to run this eval (e.g. `kd.evals.RunEvery(100)` or
      `kd.evals.RunXM()`)
    base_cfg: reference to the experiment configuration (set automatically).
  """

  # Evaluators can be used as standalone, so keep a default name
  name: str = _DEFAULT_EVAL_NAME

  # Do not resolve the RunStrategy to avoid depending on XManager
  run: konfig.ConfigDictLike[run_strategies.RunStrategy]

  base_cfg: config_lib.Trainer = dataclasses.field(
      default=config_util.ROOT_CFG_REF, repr=False
  )

  __konfig_resolve_exclude_fields__ = ('run',)

  def __post_init__(self) -> None:
    if hasattr(super(), '__post_init__'):
      super().__post_init__()  # Future proof to run `__post_init__` in parents
    if not self.name.replace('.', '_').replace('-', '_').isidentifier():
      raise ValueError(
          'Evaluator name should be a valid Python identifier. Got:'
          f' {self.name}'
      )

  def maybe_eval(self, *, step: int, state: train_step.TrainState) -> Any:
    """Run or skip the evaluator for the given train-step."""
    if self._resolved_run.should_eval_in_train(step):
      try:
        return self.evaluate(state, step)
      except Exception as e:  # pylint: disable=broad-exception-caught
        # Chain exception so we see the name of the Evaluator that failed.
        epy.reraise(e, f'Evaluator {self.name} failed at step {step}: ')

  def evaluate(self, state: train_step.TrainState, step: int) -> Any:
    """Run this evaluator then write and optionally return the results."""
    raise NotImplementedError

  @functools.cached_property
  def writer(self) -> metric_writer.KDMetricWriter:
    """Metric writer used for this evaluator."""
    return metric_writer.KDMetricWriter(
        workdir=self.base_cfg.workdir, collection=self.name
    )

  @functools.cached_property
  def _resolved_run(self) -> run_strategies.KauldronRunStrategy:
    return run_strategies.run_strategy_cfg_to_kauldron_run_info(self.run)


class Evaluator(EvaluatorBase):
  """Evaluator running `num_batches` times.

  Evaluators can be launched as separate XManager jobs (`run=kd.evals.RunXM()`)
  or along train `run=kd.evals.RunEvery(100)`.

  If not provided, losses, metrics, summaries are reused from train.

  Usage:

  ```
  evaluator = kd.evals.Evaluator(
      run=kd.evals.RunEvery(100),
      ds=test_ds,
      base_cfg=cfg,
  )
  evaluator.maybe_eval(step=0, state=state)
  ```

  Attributes:
    num_batches: How many batches to run evaluation on. Use `None` to evaluate
      on the full test dataset. Note that each evaluation reinitializes the
      dataset iterator, so setting to `1` will run all evaluations on the same
      batch.
    cache: Whether to cache the iterator
    ds: Dataset to evaluate on.
    losses: Losses
    metrics: Metrics
    summaries: Summaries
  """

  num_batches: Optional[int]
  cache: bool = False
  ds: data.Pipeline = config_util.ROOT_CFG_REF.eval_ds
  losses: dict[str, losses_lib.Loss] = config_util.ROOT_CFG_REF.train_losses
  metrics: dict[str, metrics_lib.Metric] = (
      config_util.ROOT_CFG_REF.train_metrics
  )
  summaries: dict[str, summaries_lib.Summary] = (
      config_util.ROOT_CFG_REF.train_summaries
  )
  writer: metric_writer.KDMetricWriter = dataclasses.field(
      default_factory=metric_writer.KDMetricWriter
  )

  # TODO(klausg): filter out metrics / summaries that access grads/updates

  def __post_init__(self):
    super().__post_init__()
    # set the name of the collection to eval name (unless specified otherwise)
    if self.writer.collection == metric_writer.COLLECTION_NOT_SET:
      new_writer = self.writer.replace(collection=self.name)
      object.__setattr__(self, 'writer', new_writer)

  def update_from_root_cfg(
      self: _SelfT, root_cfg: config_lib.Trainer
  ) -> _SelfT:
    """See base class."""
    new_self = super().update_from_root_cfg(root_cfg)
    if new_self.ds is None:
      raise ValueError(
          f'Eval dataset missing (`cfg.evals.{self.name}.ds is None`). Please'
          ' set it either in `kd.train.Trainer.eval_ds` or in'
          ' `Evaluator(ds=...)`.'
      )

    return new_self.replace(
        ds=new_self.ds.update_from_root_cfg(root_cfg),
        writer=new_self.writer.update_from_root_cfg(root_cfg),
    )

  @functools.cached_property
  def ds_iter(self) -> data.IterableDataset:
    """"""
    ds_iter = self.ds
    if self.num_batches is not None:
      ds_iter = ds_iter.take(self.num_batches)
    if self.cache:
      if self.num_batches is None:
        raise ValueError('Can only cache if num_batches is set.')
      ds_iter = ds_iter.cache()
    return ds_iter.device_put()

  def evaluate(
      self, state: train_step.TrainState, step: int
  ) -> train_step.Auxiliaries:
    """Run one full evaluation."""
    self._assert_root_cfg_resolved()

    merged_aux = None
    for eval_step, batch in utils.enum_iter(self.ds_iter, desc='eval'):
      eval_step = sharding.device_put(eval_step, sharding.REPLICATED)
      aux = basic_eval_step(
          model_with_aux=self.model_with_aux,
          rng_streams=self.base_cfg.rng_streams,
          eval_step=eval_step,
          state=state,
          batch=batch,
      )
      # Merge/accumulate all states
      # By default, cross-process communication is only allowed inside
      # `jax.jit` but clu metric do not support `jax.jit`:
      # https://github.com/google/CommonLoopUtils/tree/HEAD/clu/metrics.py;l=383;rcl=559340497
      # So we locally allow cross-process communication for merging the
      # metrics
      with jax.spmd_mode('allow_all'), jax.transfer_guard('allow'):
        merged_aux = merged_aux | aux
    assert merged_aux is not None  # At least one iteration

    self.writer.write_step_metrics(
        step=step,
        aux=merged_aux,
        schedules={},
        model_with_aux=self.model_with_aux,
        log_summaries=True,
    )
    return merged_aux

  @functools.cached_property
  def model_with_aux(self) -> train_step.ModelWithAux:
    """Model which also compute the auxiliaries (losses, metrics,...)."""
    return train_step.ModelWithAux(
        model=self.base_cfg.model,
        losses=self.losses,
        metrics=self.metrics,
        summaries=self.summaries,
    )


@jax_utils.jit(
    static_argnames=('model_with_aux', 'rng_streams'),
    # in_shardings=lambda: dict(  # pylint: disable=g-long-lambda
    #     eval_step=sharding.REPLICATED,
    #     state=sharding.REPLICATED,
    #     batch=sharding.SHARDED,
    # ),
    out_shardings=lambda: sharding.REPLICATED,
)
def basic_eval_step(
    *,
    model_with_aux: train_step.ModelWithAux,
    rng_streams: rngs_lib.RngStreams,
    eval_step: int,
    state: train_step.TrainState,
    batch,
) -> train_step.Auxiliaries:
  """Call the model (pmap version)."""
  _, ctx = model_with_aux.forward(
      params=state.params,
      batch=batch,
      rngs=rng_streams.eval_rngs(eval_step),
      step=state.step,  # Step is train step, NOT eval
      is_training=False,
  )
  aux = model_with_aux.get_aux(
      ctx,
      return_losses=True,
      return_metrics=True,
      return_summaries=True,
  )
  return aux


def normalize_evaluators(
    evaluators: collections.abc.Mapping[str, EvaluatorBase]
) -> collections.abc.Mapping[str, EvaluatorBase]:
  """Set the evaluator names."""
  if not isinstance(evaluators, collections.abc.Mapping):
    raise TypeError(
        '`cfg.evals` should be a `dict[str, EvaluatorBase]`. Got:'
        f' {type(evaluators)}'
    )
  return {k: _replace_name(c, k) for k, c in evaluators.items()}


def _replace_name(evaluator: EvaluatorBase, name: str) -> EvaluatorBase:
  """Set the `evaluator.name`."""
  if not isinstance(evaluator, EvaluatorBase):
    raise TypeError(
        'Eval values should be `kd.evals.EvaluatorBase`. Got:'
        f' {name}={type(evaluator)}'
    )
  elif name == 'train':
    raise ValueError(
        'Evaluator cannot be named `train` as it conflict with training'
        ' metrics.'
    )
  elif evaluator.name == _DEFAULT_EVAL_NAME:  # Default name, overwrite
    return dataclasses.replace(evaluator, name=name)
  elif evaluator.name == name:
    return evaluator
  else:
    raise ValueError(
        f'Evaluator name provided should match. Got: {evaluator.name} != {name}'
    )
