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

"""Abstract base transformations.

Those classes allow to define transformations shared between PyGrain and
TfGrain.
"""

import abc


class MapTransform(abc.ABC):
  """Abstract base class for all 1:1 transformations of elements."""

  @abc.abstractmethod
  def map(self, element):
    """Maps a single element."""


# class RandomMapTransform(abc.ABC):
#   """Abstract base class for all random 1:1 transformations of elements."""

#   @abc.abstractmethod
#   def random_map(self, element, rng: np.random.Generator):
#     """Maps a single element."""


class FilterTransform(abc.ABC):
  """Abstract base class for filter transformations for individual elements.

  The pipeline will drop any element for which the filter function returns
  False.
  """

  @abc.abstractmethod
  def filter(self, element) -> bool:
    """Filters a single element; returns True if the element should be kept."""


Transformation = MapTransform | FilterTransform