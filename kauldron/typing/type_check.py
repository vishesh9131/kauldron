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

"""Dynamic typechecking decorator."""

from __future__ import annotations

import contextlib
import dataclasses
import functools
import inspect
import re
import sys
import types
import typing
from typing import Any, Type, TypedDict, Union

from etils import edc
from etils import enp
from etils import epy
import jaxtyping
from kauldron.typing import shape_spec

with epy.lazy_imports():
  import typeguard  # pylint: disable=g-import-not-at-top


# a global switch to disable typechecking
# (e.g. for debugging or colab hacking)
TYPECHECKING_ENABLED = True

_undef = object()


def check_type(
    value: Any,
    expected_type: Any,
) -> None:
  """Ensure that value matches expected_type, alias for typeguard.check_type."""
  if True:  # Typeguard not yet supported
    return
  return typeguard.check_type(value, expected_type)


exc_cls = Exception
class TypeCheckError(exc_cls):
  """Indicates a runtime typechecking error from the @typechecked decorator."""

  def __init__(
      self,
      message: str,
      arguments: dict[str, Any],
      return_value: Any,
      annotations: dict[str, Any],
      return_annotation: Any,
      memo: shape_spec.Memo,
  ):
    super().__init__(message)
    self.arguments = arguments
    self.return_value = return_value
    self.annotations = annotations
    self.return_annotation = return_annotation
    self.memo = memo

  def __str__(self) -> str:
    msg = super().__str__()
    arg_reprs = []
    for name, value in self.arguments.items():
      ann = self.annotations[name]
      if ann is inspect.Parameter.empty:
        key_repr = name
      else:
        key_repr = f"{name}: {self._annotation_repr(ann)}"
      val_repr = _format_argument_value(value)
      arg_reprs.append(f"  {key_repr} = {val_repr}")
    args_string = "\n".join(arg_reprs)
    if self.return_value is _undef:
      return (
          f"{msg}\n\nInputs:\n{args_string}\n\n"
          f"Inferred Dims:\n {self.memo!r}\n\n"
      )
    else:
      ret_string = _format_return_values(self.return_value)
      ret_ann = self._annotation_repr(self.return_annotation)
      return (
          f"{msg}\n\nInputs:\n{args_string}\n\n"
          f"Return -> {ret_ann}:\n{ret_string}\n\n"
          f"Inferred Dims:\n {self.memo!r}\n\n"
      )

  @staticmethod
  def _annotation_repr(ann: Any) -> str:
    # TODO(klausg): handle more complex annotations (e.g. TypedDict)
    # TODO(klausg): cleanup
    shape_ann = ann
    if typing.get_origin(ann) == types.UnionType:
      shape_ann = ann.__args__[0]
    if hasattr(shape_ann, "_kd_repr"):
      return shape_ann._kd_repr  # pylint: disable=protected-access
    else:
      return repr(ann)


def typechecked(fn):
  """Decorator to enable runtime type-checking and shape-checking."""
  if True:  # Typeguard not yet supported
    return fn

  if hasattr(fn, "__wrapped__"):
    raise AssertionError("@typechecked should be the innermost decorator")

  @epy.maybe_reraise(lambda: f"Error in {fn.__qualname__}: ")
  @jaxtyping.jaxtyped(typechecker=None)
  @functools.wraps(fn)
  def _reraise_with_shape_info(*args, _typecheck: bool = True, **kwargs):
    if not (TYPECHECKING_ENABLED and _typecheck):
      # typchecking disabled globally or locally -> just return fn(...)
      return fn(*args, **kwargs)

    # Find either the first Python wrapper or the actual function
    python_func = inspect.unwrap(fn, stop=lambda f: hasattr(f, "__code__"))
    # manually reproduce the functionality of typeguard.typechecked, so that
    # we get access to the returnvalue of the function
    localns = sys._getframe(1).f_locals  # pylint: disable=protected-access
    with _checker.activate():
      memo = typeguard.CallMemo(python_func, localns, args=args, kwargs=kwargs)
      retval = _undef
      try:
        typeguard.check_argument_types(memo)
        retval = fn(*args, **kwargs)
        typeguard.check_return_type(retval, memo)
        return retval
      except typeguard.TypeCheckError as e:
        # Use function signature to construct a complete list of named arguments
        sig = inspect.signature(fn)
        bound_args = sig.bind(*args, **kwargs)
        bound_args.apply_defaults()

        annotations = {k: p.annotation for k, p in sig.parameters.items()}
        # TODO(klausg): filter the stacktrace to exclude all the typechecking
        raise TypeCheckError(
            str(e),
            arguments=bound_args.arguments,
            return_value=retval,
            annotations=annotations,
            return_annotation=sig.return_annotation,
            memo=shape_spec.Memo.from_current_context(),
        ) from e

  return _reraise_with_shape_info


def _format_argument_value(val):
  if isinstance(val, bool | str | int | float | complex | None):
    # show values for simple types
    return repr(val)
  if enp.ArraySpec.is_array(val):
    # show ArraySpec for arrays (e.g. f32[32, 32, 3])
    return str(enp.ArraySpec.from_array(val))
  else:
    # try repr and if it is too long use the type
    r = repr(val)
    return repr(type(val)) if len(r) > 76 else r


def _format_return_values(return_value):
  """Format a given return value for use in TypeCheckError."""
  arg_reprs = []
  if isinstance(return_value, dict):
    for name, value in return_value.items():
      val_repr = _format_argument_value(value)
      arg_reprs.append(f"  {name} : {val_repr}")
  elif isinstance(return_value, (list, tuple)):
    for i, value in enumerate(return_value):
      val_repr = _format_argument_value(value)
      arg_reprs.append(f"  [{i}] : {val_repr}")
  else:
    arg_reprs.append(_format_argument_value(return_value))
  return "\n".join(arg_reprs)


@dataclasses.dataclass(frozen=True)
class ArraySpecMatch:
  """Detailed match of a particular value against an array specification.

  Attributes:
    value: Any array instance
    array_spec: A kauldron array annotation (e.g. kd.typing.Float["b h w 3"])
  """

  value: Any
  array_spec: Type[jaxtyping.AbstractArray]

  @functools.cached_property
  def type_correct(self) -> bool:
    """Whether the value matches the type from the array spec."""
    # e.g. numpy vs tensorflow
    return isinstance(self.value, self.array_spec.array_type)

  @functools.cached_property
  def dtype_correct(self) -> bool:
    """Whether the value.dtype matches the allowed dtypes of the array_spec."""
    # This method duplicates some functionality of __isinstance__ in jaxtyping.
    # This is necessary because the dtype checking cannot be called separately
    # of __isinstance__ which may modify the memo stack.
    # See jaxtyping._array_types._MetaAbstractArray.__instancecheck__
    # https://github.com/google/jaxtyping/tree/HEAD/jaxtyping/_array_types.py;l=141
    if self.array_spec.dtypes is jaxtyping._array_types._any_dtype:  # pylint: disable=protected-access
      return True

    dtype = _get_dtype_str(self.value)
    for cls_dtype in self.array_spec.dtypes:
      if type(cls_dtype) is str:  # pylint: disable=unidiomatic-typecheck
        if dtype == cls_dtype:
          return True
      elif type(cls_dtype) is re.Pattern:  # pylint: disable=unidiomatic-typecheck
        if cls_dtype.match(dtype):
          return True
      else:
        raise TypeError(f"got unsupported dtype spec {cls_dtype}")
    return False

  @functools.cached_property
  def shape_correct(self) -> bool:
    """Whether value.shape matches the allowed shapes of the array_spec."""
    return self.all_correct  # TODO(klausg): temorarily disable shape-checks

  @functools.cached_property
  def all_correct(self) -> bool:
    """Whether the value fully matches the array_spec."""
    return isinstance(self.value, self.array_spec)
    # return self.type_correct and self.dtype_correct and self.shape_correct

  @functools.cached_property
  def is_interesting(self) -> bool:
    """Whether this is an interesting match failure."""
    if not self.type_correct:
      # Wrong array type entries are only interesting if they match otherwise.
      return self.dtype_correct  # TODO(klausg): and self.shape_correct
    elif not self.dtype_correct and not self.shape_correct:
      # Entries that do not match at all are not interesting.
      return False
    return True

  def fail_message(self) -> str:
    """Return a message explaining the most salient failure of this match."""
    if hasattr(self.array_spec, "_kd_repr"):
      array_spec_repr = self.array_spec._kd_repr  # pylint: disable=protected-access
    else:
      array_spec_repr = self.array_spec.__name__
    if not self.type_correct:
      return (
          f"{array_spec_repr} because array type {type(self.value)} is not an"
          f" instance of {self.array_spec.array_type})"
      )
    if not self.dtype_correct:
      return (
          f"{array_spec_repr} because of dtype ({_get_dtype_str(self.value)}"
          f" not in {self.array_spec.dtypes})"
      )
    if not self.shape_correct:
      return (
          f"{array_spec_repr} because of shape"
          f" ({self.value.shape} incompatible with '{self.array_spec.dim_str}')"
      )
    return f"{array_spec_repr} matches"  # shouldn't happen


def _custom_array_type_union_checker(
    value: Any,
    origin_type: Any,
    args: tuple[Any, ...],
    memo: typeguard.TypeCheckMemo,
) -> None:
  """Custom checker for typeguard to better support Array type annotations."""
  del origin_type, memo
  individual_matches = [ArraySpecMatch(value, arg) for arg in args]
  correct_matches = [m.all_correct for m in individual_matches]
  if any(correct_matches):
    # There is a correct match -> no error
    # run isinstance check to modify the memo-stack
    idx = correct_matches.index(True)
    assert isinstance(value, individual_matches[idx].array_spec)
    # TODO(klausg): if multiple matches with conflicting shapes -> raise error
    return  # There is a correct match -> no error

  # first check if any of the array types matches
  if not any(m.type_correct for m in individual_matches):
    acceptable_array_types = {arg.array_type for arg in args}
    raise typeguard.TypeCheckError(
        f"was of type {type(value)} which is none of {acceptable_array_types}"
    )

  # then check if any of the dtypes matches
  value_spec_str = _format_argument_value(value)
  if not any(m.dtype_correct for m in individual_matches):
    acceptable_dtypes = list({dtype for arg in args for dtype in arg.dtypes})  # pylint: disable=g-complex-comprehension
    if len(acceptable_dtypes) > 1:
      options_str = f"any of {acceptable_dtypes}"
    else:
      options_str = f"{acceptable_dtypes[0]}"
    raise typeguard.TypeCheckError(
        f"was {value_spec_str} which is not dtype-compatible with {options_str}"
    )
  # then check if any of the shapes matches
  if not any(m.shape_correct for m in individual_matches):
    acceptable_shapes = list({arg.dim_str for arg in args})
    if len(acceptable_shapes) > 1:
      options_str = f"any of {acceptable_shapes}"
    else:
      options_str = f"'{acceptable_shapes[0]}'"
    raise typeguard.TypeCheckError(
        f"was {value_spec_str} which is not shape-compatible with {options_str}"
    )

  # None of the three factors alone fail, but a combination of them does.
  # That means we compile a list of interesting failures:
  fail_messages = "\n".join(
      "  - " + m.fail_message() for m in individual_matches if m.is_interesting
  )
  raise typeguard.TypeCheckError(
      f"was {value_spec_str} which did not match any of:\n{fail_messages}"
  )


def _get_dtype_str(value) -> str:
  """Get value dtype as a string for any array (np, jnp, tf, torch)."""
  return str(enp.lazy.dtype_from_array(value))


def _is_array_type(origin_type) -> bool:
  try:
    return inspect.isclass(origin_type) and issubclass(
        origin_type, jaxtyping.AbstractArray
    )
  except TypeError:
    # If a type doesn't support isclass or issubclass it is not an array type.
    return False


def _match_any(
    value: Any,
    origin_type: Any,
    args: tuple[Any, ...],
    memo: typeguard.TypeCheckMemo,
) -> None:
  del value, origin_type, args, memo
  return None  # Any always matches, never raise an exception


def _array_spec_checker_lookup(
    origin_type: Any, args: tuple[Any, ...], extras: tuple[Any, ...]
) -> typeguard.TypeCheckerCallable | None:
  """Lookup function to register custom array type checkers in typeguard."""
  del extras
  if not _checker.is_active:
    return None
  if origin_type in [Union, types.UnionType]:
    # TODO(klausg): handle Union of ArrayType with other types
    if all(_is_array_type(arg) for arg in args):
      return _custom_array_type_union_checker
  if origin_type is Any:
    # By default typeguard doesn't support Any annotations
    # this is a workaround.
    return _match_any
  return None


def _custom_dataclass_checker(
    value: Any,
    origin_type: Any,
    args: tuple[Any, ...],
    memo: typeguard.TypeCheckMemo,
) -> None:
  """Custom checker for typeguard to better support dataclass annotations."""
  del args
  # Check if the value is of the right type.
  if not isinstance(value, origin_type):
    raise typeguard.TypeCheckError(
        f"was of type {type(value)} which is not {origin_type}"
    )
  # Convert dataclass values and annotations into a TypedDict.
  fields = dataclasses.fields(origin_type)
  dataclass_as_typed_dict = TypedDict(
      "dataclass_as_typed_dict",
      {f.name: f.type for f in fields},
  )  # pytype: disable=wrong-arg-types
  values = {k.name: getattr(value, k.name) for k in fields}
  try:
    return typeguard.check_type(
        dataclass_as_typed_dict(**values),
        dataclass_as_typed_dict,
        memo=memo,
    )
  # TODO(thomaskeck) Avoid causing NameErrors.
  # Ignore NameErrors, that can happen if a dataclass contains a Generic TypeVar
  # annotation that cannot be resolved.
  except NameError:
    pass


def _dataclass_checker_lookup(
    origin_type: Any, args: tuple[Any, ...], extras: tuple[Any, ...]
) -> typeguard.TypeCheckerCallable | None:
  """Lookup function to register custom dataclass checkers in typeguard."""
  del args, extras
  if not _checker.is_active:
    return None
  if dataclasses.is_dataclass(origin_type):
    return _custom_dataclass_checker
  return None


def add_custom_checker_lookup_fn(lookup_fn):
  """Add custom array spec checker lookup function to typeguard."""
  # Add custom array spec checker lookup function to typguard
  # check not for equality but for qualname, to avoid many copies when
  # reloading modules from colab
  if hasattr(typeguard, "checker_lookup_functions"):
    # Recent `typeguard` has different API
    checker_lookup_fns = typeguard.checker_lookup_functions
  else:
    # TODO(epot): Remove once typeguard is updated
    checker_lookup_fns = typeguard.config.checker_lookup_functions
  for i, f in enumerate(checker_lookup_fns):
    if f.__qualname__ == lookup_fn.__qualname__:
      # replace
      checker_lookup_fns[i : i + 1] = [lookup_fn]
      break
  else:  # prepend
    checker_lookup_fns[:0] = [lookup_fn]
