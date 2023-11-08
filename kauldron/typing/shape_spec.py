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

"""Parser for jaxtyping-like shape specs."""

from __future__ import annotations

import abc
import dataclasses
import enum
import itertools
import math
import operator
import typing
from typing import Any, List, Optional, Callable

import jaxtyping
import lark


if typing.TYPE_CHECKING:
  Shape = tuple[int, ...]
else:

  class Shape(tuple):
    """Helper to construct concrete shape tuples from shape-specs.

    Example:
    ```
    @typchecked
    def foo(x: Float["*b h w c"], y: Float["h w c"]):
      isinstance(x, Float["h w"])
      print(Shape("h w/2 c+1"))

    foo(np.zeros((7, 32, 32, 3)))  # prints (32, 16, 4)
    ```
    """

    def __new__(cls, spec_str: str) -> tuple[int, ...]:
      spec = parse_shape_spec(spec_str)
      memo = Memo.from_current_context()
      return spec.evaluate(memo)


# try grammar online: https://www.lark-parser.org/ide/#
shape_parser = lark.Lark(
    start="shape_spec",
    regex=True,
    grammar=r"""
// shape_spe is a list of dim_specs separated by whitespace
// e.g. "*b h w//2 3"
shape_spec: (_WS_INLINE* dim_spec)? (_WS_INLINE+ dim_spec)*

?dim_spec: expr
         | var_dim
         | other_dim

// Dim expressions are sub-structured into term, factor, unary, power, and atom
// to account for operator precedence:
// expr (lowest precedence): sum operations (+, -)
?expr: term
     | term SUM_OP expr    -> binary_op
SUM_OP: "+" | "-"

// multiplication operations (*, /, //, %)
?term: unary
     | unary MUL_OP term   -> binary_op
MUL_OP: "*" | "/" | "//" | "%"

// unary operators (we only support "-", not "+" or "~")
?unary: power
      | "-" unary          -> neg

// raising a value to the power of another (**)
?power: atom
      | atom POW_OP unary  -> binary_op
POW_OP: "**"

// atoms (highest precedence): include ints, named dims,  parenthesized
// expressions, and functions.
?atom: INT  -> int_dim
     | NAME -> name_dim
     | "(" expr ")"
     | FUNC "(" arg_list ")"  -> func

FUNC: "min" | "max" | "sum" | "prod"


// named variadic dim spec (can be part of a function)
var_dim: "*" NAME

// Other dim specs (cannot be part of an expression)
other_dim: "_" NAME?    -> anon_dim
         | "..."        -> anon_var_dim
         | "*_" NAME?   -> anon_var_dim
         | "#" NAME     -> broadcast_dim
         | "#" INT      -> broadcast_dim
         | "#*" NAME    -> broadcast_var_dim
         | "*#" NAME    -> broadcast_var_dim

// argument list for min, max, sum etc. can be either
//   - a single variadic dim e.g. min(*channel)
//   - a list of at least two normal dims e.g. min(a,b,c)
//     (but not a single normal dim like min(a))
//   - a combination: e.g. sum(a,*b)
?arg_list: expr ("," (expr | var_dim))+
         | var_dim ("," (expr | var_dim))*

// TODO: maybe add composition to atom?
// composition: "(" name_dim (_WS_INLINE (name_dim | var_dim))+ ")"
//            | "(" var_dim (_WS_INLINE (name_dim | var_dim))* ")"



// dimension names consist of letters, digits and underscores but have to start
// with a letter (underscores are used to indicate anonymous dims)
NAME: LETTER ("_"|LETTER|DIGIT)*

_WS_INLINE: (" "|/\t/)+

%import common.INT
%import common.LETTER
%import common.DIGIT
""",
)


class ShapeError(ValueError):
  pass


class _Priority(enum.IntEnum):
  ADD = enum.auto()
  MUL = enum.auto()
  POW = enum.auto()
  UNARY = enum.auto()
  ATOM = enum.auto()


class DimSpec(abc.ABC):

  def evaluate(self, memo: Memo) -> tuple[int, ...]:
    raise NotImplementedError()

  @property
  def priority(self) -> int:
    return _Priority.ATOM


@dataclasses.dataclass(init=False)
class ShapeSpec:
  """Parsed shape specification."""
  dim_specs: tuple[DimSpec, ...]

  def __init__(self, *dim_specs: DimSpec):
    self.dim_specs = tuple(dim_specs)

  def evaluate(self, memo: Memo) -> tuple[int, ...]:
    return tuple(
        itertools.chain.from_iterable(s.evaluate(memo) for s in self.dim_specs)
    )

  def __repr__(self):
    return " ".join(repr(ds) for ds in self.dim_specs)


@dataclasses.dataclass
class IntDim(DimSpec):
  value: int
  broadcastable: bool = False

  def evaluate(self, memo: Memo) -> tuple[int, ...]:
    if self.broadcastable:
      raise ShapeError(f"Cannot evaluate a broadcastable dim: {self!r}")
    return (self.value,)

  def __repr__(self):
    prefix = "_" if self.broadcastable else ""
    return prefix + str(self.value)


@dataclasses.dataclass
class SingleDim(DimSpec):
  """Simple individual dimensions like "height", "_a" or "#c"."""

  name: Optional[str] = None
  broadcastable: bool = False
  anonymous: bool = False

  def evaluate(self, memo: Memo) -> tuple[int, ...]:
    if self.anonymous:
      raise ShapeError(f"Cannot evaluate anonymous dimension: {self!r}")
    elif self.broadcastable:
      raise ShapeError(f"Cannot evaluate a broadcastable dimension: {self!r}")
    elif self.name not in memo.single:
      raise ShapeError(
          f"No value known for {self!r}. "
          f"Known values are: {sorted(memo.single.keys())}"
      )
    else:
      return (memo.single[self.name],)

  def __repr__(self):
    return (
        ("#" if self.broadcastable else "")
        + ("_" if self.anonymous else "")
        + (self.name if self.name else "")
    )


@dataclasses.dataclass
class VariadicDim(DimSpec):
  """Variable size dimension specs like "*batch" or "..."."""

  name: Optional[str] = None
  anonymous: bool = False
  broadcastable: bool = False

  def evaluate(self, memo: Memo) -> tuple[int, ...]:
    if self.anonymous:
      raise ShapeError(f"Cannot evaluate anonymous dimension: {self!r}")
    if self.broadcastable:
      raise ShapeError(
          f"Cannot evaluate a broadcastable variadic dimension: {self!r}"
      )
    if self.name not in memo.variadic:
      raise ShapeError(
          f"No value known for {self!r}. Known values are:"
          f" {sorted(memo.variadic.keys())}"
      )
    return memo.variadic[self.name]

  def __repr__(self):
    if self.anonymous:
      return "..."
    if self.broadcastable:
      return "*#" + self.name
    else:
      return "*" + self.name


BinOp = Callable[[Any, Any], Any]


@dataclasses.dataclass
class Operator:
  symbol: str
  fn: BinOp
  priority: _Priority


OPERATORS = [
    Operator("+", operator.add, _Priority.ADD),
    Operator("-", operator.sub, _Priority.ADD),
    Operator("*", operator.mul, _Priority.MUL),
    Operator("/", operator.truediv, _Priority.MUL),
    Operator("//", operator.floordiv, _Priority.MUL),
    Operator("%", operator.mod, _Priority.MUL),
    Operator("**", operator.pow, _Priority.POW),
]

SYMBOL_2_OPERATOR = {o.symbol: o for o in OPERATORS}


@dataclasses.dataclass
class Memo:
  """Jaxtyping information about the shapes in the current scope."""

  single: dict[str, int]
  variadic: dict[str, tuple[int, ...]]

  @classmethod
  def from_current_context(cls):
    """Create a Memo from the current typechecking context."""
    # TODO(klausg): tidy this up once the jaxtyping PR chain is done
    single_memo, variadic_memo = {}, {}
    try:
      single_memo, variadic_memo, *_ = jaxtyping._storage.get_shape_memo()  # pylint: disable=protected-access # pytype: disable=module-attr
    except Exception:  # pylint: disable=broad-exception-caught
      if hasattr(jaxtyping._decorator, "storage"):  # pylint: disable=protected-access
        storage = jaxtyping._decorator.storage  # pylint: disable=protected-access
      else:
        storage = jaxtyping._decorator._storage  # pylint: disable=protected-access
      memo_stack = getattr(storage, "memo_stack", [])  # pylint: disable=protected-access
      if memo_stack:
        single_memo, variadic_memo, *_ = memo_stack[-1]

    def _maybe_remove_bool(memo):
      match memo:
        case (bool(_), (*dims,)) if all(isinstance(d, int) for d in dims):
          return tuple(dims)
        case (*dims,) if all(isinstance(d, int) for d in dims):
          return tuple(dims)
        case _:
          raise ValueError(f"Unexpected variadic memo: {memo!r}")

    variadic_memo = {
        k: _maybe_remove_bool(memo) for k, memo in variadic_memo.items()
    }
    return cls(
        single=single_memo.copy(),
        variadic=variadic_memo.copy(),
    )

  def __repr__(self) -> str:
    out = {k: v for k, v in self.single.items()}
    out.update({f"*{k}": v for k, v in self.variadic.items()})
    return repr(out)


@dataclasses.dataclass
class FunctionDim(DimSpec):
  """Function based dimension specs like "min(a,b)" or "sum(*batch)."""

  name: str
  fn: Callable[..., int]
  arguments: list[DimSpec]

  def evaluate(self, memo: Memo) -> tuple[int]:
    vals = itertools.chain.from_iterable(
        arg.evaluate(memo) for arg in self.arguments
    )
    return (self.fn(vals),)

  def __repr__(self):
    arg_list = ",".join(repr(a) for a in self.arguments)
    return f"{self.name}({arg_list})"


NAME_2_FUNC = {"sum": sum, "min": min, "max": max, "prod": math.prod}


@dataclasses.dataclass
class BinaryOpDim(DimSpec):
  """Binary ops for dim specs such as "H*W" or "C+1"."""

  op: Operator
  left: DimSpec
  right: DimSpec

  def evaluate(self, memo: Memo) -> tuple[int]:
    (left,) = self.left.evaluate(memo)  # unpack tuple (has to be 1-dim)
    (right,) = self.right.evaluate(memo)  # unpack tuple (has to be 1-dim)
    return (self.op.fn(left, right),)

  @property
  def priority(self) -> int:
    return self.op.priority

  def __repr__(self):
    left_repr = (
        repr(self.left)
        if self.priority < self.left.priority
        else f"({self.left!r})"
    )
    right_repr = (
        repr(self.right)
        if self.priority < self.right.priority
        else f"({self.right!r})"
    )
    return f"{left_repr}{self.op.symbol}{right_repr}"


@dataclasses.dataclass
class NegDim(DimSpec):
  """Negation of a dim spec, e.g. "-h"."""

  child: DimSpec

  def evaluate(self, memo: Memo) -> tuple[int]:
    return (-self.child.evaluate(memo)[0],)

  @property
  def priority(self) -> int:
    return _Priority.UNARY

  def __repr__(self):
    if self.priority < self.child.priority:
      return f"-{self.child!r}"
    else:
      return f"-({self.child!r})"


class ShapeSpecTransformer(lark.Transformer):
  """Transform a lark.Tree into a ShapeSpec."""

  @staticmethod
  def shape_spec(args: List[DimSpec]) -> ShapeSpec:
    return ShapeSpec(*args)

  @staticmethod
  def int_dim(args: List[Any]) -> IntDim:
    return IntDim(value=int(args[0]))

  @staticmethod
  def name_dim(args: List[Any]) -> SingleDim:
    return SingleDim(name=args[0])

  @staticmethod
  def anon_dim(args: List[Any]) -> SingleDim:
    name = args[0] if args else None
    return SingleDim(name=name, anonymous=True)

  @staticmethod
  def anon_var_dim(args: List[Any]) -> VariadicDim:
    name = args[0] if args else None
    return VariadicDim(name=name, anonymous=True)

  @staticmethod
  def var_dim(args: List[Any]) -> VariadicDim:
    return VariadicDim(name=args[0])

  @staticmethod
  def broadcast_dim(args: List[Any]) -> DimSpec:
    try:
      return IntDim(value=int(args[0]), broadcastable=True)
    except ValueError:
      return SingleDim(name=args[0], broadcastable=True)

  @staticmethod
  def broadcast_var_dim(args: List[Any]) -> VariadicDim:
    return VariadicDim(name=args[0], broadcastable=True)

  @staticmethod
  def binary_op(args: List[Any]) -> BinaryOpDim:
    left, op, right = args
    return BinaryOpDim(left=left, right=right, op=SYMBOL_2_OPERATOR[str(op)])

  @staticmethod
  def neg(args: List[Any]) -> NegDim:
    return NegDim(child=args[0])

  @staticmethod
  def func(args: List[Any]) -> FunctionDim:
    name, arguments = args
    return FunctionDim(name=name, fn=NAME_2_FUNC[name], arguments=arguments)

  @staticmethod
  def arg_list(args: List[Any]) -> List[Any]:
    return args


def parse_shape_spec(spec: str) -> ShapeSpec:
  tree = shape_parser.parse(spec)
  return ShapeSpecTransformer().transform(tree)
