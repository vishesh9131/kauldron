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

"""Histogram summaries."""

from __future__ import annotations

import dataclasses

from etils import etree
import flax
from kauldron import kontext
from kauldron import metrics
from kauldron.typing import Array, typechecked  # pylint: disable=g-multiple-import,g-importing-member


@dataclasses.dataclass(kw_only=True, frozen=True, eq=True)
class Histogram:
  """Output type for histogram summaries."""

  tensor: Array["n"]
  num_buckets: int


@dataclasses.dataclass(kw_only=True, frozen=True, eq=True)
class HistogramSummary(metrics.Metric):
  """Basic histogram summary."""

  tensor: kontext.Key
  num_buckets: int = 30

  @flax.struct.dataclass
  class State(metrics.CollectingState["HistogramSummary"]):
    """Collecting state that returns Histograms."""

    tensor: Array["n"]

    @typechecked
    def compute(self) -> Array["n"]:
      """Returns the concatenated and flattened values as a `Histogram`."""
      tensor = super().compute().tensor.reshape((-1,))
      if tensor.size == 0:
        raise ValueError(
            f"Histogram summary for {self.parent!r} is an empty array "
            f"tensor={etree.spec_like(tensor)}."
        )
      return Histogram(
          tensor=tensor,
          num_buckets=self.parent.num_buckets,
      )

  @typechecked
  def get_state(self, tensor: Array["*any"]) -> HistogramSummary.State:
    return self.State(tensor=tensor.reshape((-1,)))
