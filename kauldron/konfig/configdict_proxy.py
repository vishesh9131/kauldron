# Copyright 2023 The kauldron Authors.
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

"""Implementation of `ProxyObject` which resolve to `ConfigDict`."""

from __future__ import annotations

from collections.abc import Callable, Mapping
import dataclasses
import functools
import importlib
import itertools
import typing
from typing import Any, TypeVar

from etils import epy
import flax
from kauldron.konfig import configdict_base
from kauldron.konfig import fake_import_utils
import ml_collections


_T = TypeVar('_T')
_FnT = TypeVar('_FnT')

# * `{'__qualname__': 'xxx'}`: Resolved as `xxx()`
# * `{'__const__': 'xxx'}`: Resolved as `xxx`
QUALNAME_KEY = '__qualname__'
CONST_KEY = '__const__'


@dataclasses.dataclass(eq=False, repr=False)
class ConfigDictProxyObject(fake_import_utils.ProxyObject, dict):
  """Implementation of `ProxyObject` which resolve to `ConfigDict`."""

  def __new__(cls, items=None, **kwargs):
    # Support tree map functions
    # Inside `tree.map`, the classes are created as: `type(obj)(obj.items())`
    if items is not None:
      assert not kwargs
      return dict(items)
    return super().__new__(cls, **kwargs)

  def __post_init__(self):
    # `ConfigDictProxyObject` act as a constant, when assigned in another
    # configdict attribute
    dict.__init__(self, {CONST_KEY: self.qualname})

  def __call__(self, *args, **kwargs) -> ml_collections.ConfigDict:
    """`my_module.MyObject()`."""
    args_kwargs = {
        str(i): v for i, v in enumerate(args)
    }
    return configdict_base.ConfigDict({
        QUALNAME_KEY: self.qualname,
        **args_kwargs,
        **kwargs,
    })

  # Overwritte `dict` methods
  def __bool__(self) -> bool:
    return True

  __eq__ = object.__eq__
  __hash__ = object.__hash__


@typing.overload
def resolve(cfg: ml_collections.ConfigDict, *, freeze: bool = ...) -> Any:
  ...


@typing.overload
def resolve(cfg: _T, *, freeze: bool = ...) -> _T:
  ...


def resolve(cfg, *, freeze=True):
  """Recursively parses a nested ConfigDict and resolves module constructors.

  Args:
    cfg: The config to resolved
    freeze: If `True` (default), `list` are converted to `tuple`,
      `dict`/`ConfigDict` are converted to `flax.core.FrozenDict`.

  Returns:
    The resolved config.
  """
  return _ConstructorResolver(freeze=freeze)._resolve_value(cfg)  # pylint: disable=protected-access


class _ConfigDictVisitor:
  """Class which recursivelly inspect/transform a ConfigDict.

  By default, the visitor is a no-op:

  ```python
  assert _ConfigDictVisitor.apply(cfg) == cfg
  ```

  Child can overwritte specific `_resolve_xyz` function to apply specific
  transformations.
  """

  def __init__(self, freeze=True):
    self._freeze = freeze
    self._types_to_resolver = {
        (dict, ml_collections.ConfigDict): self._resolve_dict,
        (list, tuple): self._resolve_sequence,
    }

  def _resolve_value(self, value):
    """Apply the visitor/transformation to the config dict."""
    for cls, resolver_fn in self._types_to_resolver.items():
      if isinstance(value, cls):
        return resolver_fn(value)
    return self._resolve_leaf(value)  # Leaf value

  def _resolve_sequence(self, value):
    cls = type(value)
    if self._freeze:
      if cls not in (list, tuple):
        raise TypeError(f'Cannot freeze unknown sequence type {type(cls)}')
      cls = tuple
    return cls(
        [
            _reraise_with_info(self._resolve_value, i)(v)
            for i, v in enumerate(value)
        ]
    )

  def _resolve_dict(self, value):
    cls = type(value)
    if self._freeze:
      cls = flax.core.FrozenDict
    return cls(
        {
            k: _reraise_with_info(self._resolve_value, k)(v)
            for k, v in value.items()
        }
    )

  def _resolve_leaf(self, value):
    return value


class _ConstructorResolver(_ConfigDictVisitor):
  """Instanciate all `ConfigDict` proxy object."""

  def _resolve_dict(self, value):

    # Dict proxies have `__const__` or `__qualname__` keys
    if QUALNAME_KEY in value:
      if CONST_KEY in value:
        raise ValueError(
            f'Conflict: Both {QUALNAME_KEY} and {CONST_KEY} are set. For'
            f' {value}'
        )
      qualname_key = QUALNAME_KEY
    elif CONST_KEY in value:
      qualname_key = CONST_KEY
    else:
      return super()._resolve_dict(value)

    kwargs = dict(value.items())

    constructor = _import_constructor(kwargs.pop(qualname_key))

    if qualname_key == CONST_KEY:
      if kwargs:
        raise ValueError(
            f'Malformated constant: {kwargs}. Should only contain a single key.'
        )
      return constructor  # Constant are returned as-is

    kwargs = {
        k: _reraise_with_info(self._resolve_value, k)(v)
        for k, v in kwargs.items()
    }
    args = [kwargs.pop(str(i)) for i in range(num_args(kwargs))]
    return constructor(*args, **kwargs)


def _import_constructor(qualname_str: str) -> Callable[..., Any]:
  """Fix the import constructors."""
  match qualname_str.split(':'):
    case [import_str, attributes]:
      pass
    case [qualname_str]:  # Otherwise, assume single attribute
      import_str, attributes = qualname_str.rsplit('.', maxsplit=1)
    case _:
      raise ValueError(f'Invalid {qualname_str!r}')

  obj = importlib.import_module(import_str)
  for attr in attributes.split('.'):
    obj = getattr(obj, attr)
  return obj  # pytype: disable=bad-return-type


def num_args(obj: Mapping[str, Any]) -> int:
  """Returns the number of positional arguments of the callable."""
  for arg_id in itertools.count():
    if str(arg_id) not in obj:
      break
  return arg_id  # pylint: disable=undefined-loop-variable,undefined-variable


def _reraise_with_info(fn: _FnT, info: str | int) -> _FnT:
  @functools.wraps(fn)
  def decorated(*args, **kwargs):
    try:
      return fn(*args, **kwargs)
    except Exception as e:  # pylint: disable=broad-exception-caught
      info_ = f'[{info}]' if isinstance(info, int) else repr(info)
      epy.reraise(e, prefix=f'In {info_}:\n')

  return decorated
