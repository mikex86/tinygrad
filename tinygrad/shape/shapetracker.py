# ShapeTracker allows movement operations to a buffer that don't require a copy to be made.
from __future__ import annotations
import functools, itertools, operator
from dataclasses import dataclass
from typing import Tuple, List, Optional, Dict, Set, cast, Union, Iterable
from tinygrad.helpers import prod, merge_dicts, getenv
from tinygrad.shape.symbolic import Variable, MulNode, Node, SumNode, NumNode, sint
from tinygrad.shape.view import View, _merge_dims

def expr_node_mask(view:View, idx:Node, valid:Optional[Node]=None) -> Node:
  expr = [valid] if valid is not None else []
  if view.mask is not None:
    acc = 1
    for d,(x,y) in zip(reversed(view.shape), reversed(view.mask)):
      if (x,y) != (0,d):
        base = ((idx//acc)%d)
        expr += [base >= x, base < y]
      acc *= d
  return Node.ands(expr)

# generate an expression if you have a single idx variable
def expr_node(view:View, idx:Optional[Node]=None) -> Node:
  if idx is None: idx = Variable('idx', 0, prod(view.shape)-1)
  ret: List[Node] = [NumNode(view.offset) if isinstance(view.offset, int) else view.offset] if view.offset else []
  acc = 1
  for d,s,_ in reversed(_merge_dims(view.shape, view.strides)):
    ret.append(((idx//acc)%d)*s)
    acc *= d
  return Node.sum(ret)

# generate an expression if you have a variable or expression for each index
def expr_idxs(view:View, idxs:Tuple[Node, ...]) -> Node:
  assert len(idxs) == len(view.shape), f"need an idx for all dimensions {idxs} vs {view.shape}"
  return Node.sum([NumNode(view.offset) if isinstance(view.offset, int) else view.offset] + [idx*st for idx,sh,st in zip(idxs, view.shape, view.strides) if sh != 1 and st != 0])  # noqa: E501

@functools.lru_cache(maxsize=None)
def merge_views(vm2:View, vm1:View) -> Optional[View]:
  if vm1.contiguous and vm1.shape == vm2.shape: return vm2
  if vm2.contiguous: return vm1
  if vm2.mask or vm1.offset != 0: return None  # this isn't supported yet
  if None in (strides := ShapeTracker((vm2, vm1)).real_strides()): return None
  return View.create(vm1.shape, cast(Tuple[sint, ...], strides), vm2.offset, vm1.mask)

@functools.lru_cache(maxsize=None)
def idxs_to_idx(shape:Tuple[int, ...], idxs:Tuple[Node, ...]) -> Node:
  assert len(idxs) == len(shape), "need an idx for all dimensions"
  # idxs[-1] * 1 + idxs[-2] * shape[-1] + idxs[-3] * shape[-1] * shape[-2] + ...
  accs = itertools.accumulate(reversed(shape[1:]), operator.mul, initial=1)
  return Node.sum([idx * acc for idx, acc in zip(reversed(idxs), accs)])

@dataclass(frozen=True)
class ShapeTracker:
  views: Tuple[View, ...]

  def __add__(self, st:ShapeTracker) -> ShapeTracker:
    ret = self
    for v in st.views: ret = ShapeTracker(ret.views + (v,)).simplify() # one view at a time = better simplification
    return ret

  def invert(self, out_shape:Tuple[sint, ...]) -> Optional[ShapeTracker]:
    ret = tuple(v.invert(s) for v,s in zip(self.views[::-1], [x.shape for x in self.views[::-1][1:]]+[out_shape]))
    return ShapeTracker(cast(Tuple[View, ...], ret)).reshape(out_shape) if all(x is not None for x in ret) else None

  @staticmethod
  def from_shape(shape:Tuple[sint, ...]): return ShapeTracker((View.create(shape),))

  @property
  def contiguous(self) -> bool: return len(self.views) == 1 and self.views[0].contiguous

  @property
  def shape(self) -> Tuple[sint, ...]: return self.views[-1].shape

  @property
  def size(self) -> int: return self.views[-1].size()

  def real_size(self) -> int:
    if 0 in self.shape: return 0
    ret = self.expr_idxs()[0].max
    while not isinstance(ret, int): ret = ret.max    # TODO: this is a while loop?!? it should be more clear what max does
    assert isinstance(ret, int), f"ret must be integer, {ret=} isn't"
    return ret+1

  def vars(self) -> Set[Variable]: return set.union(*[v.vars() for v in self.views], set())

  @property
  def var_vals(self) -> Dict[Variable, int]: return merge_dicts([dict([v.unbind()]) for v in self.vars()])

  def unbind(self) -> Tuple[ShapeTracker, Dict[Variable, int]]:
    unbound_views, var_vals = zip(*[v.unbind() for v in self.views])
    return ShapeTracker(tuple(unbound_views)), merge_dicts(var_vals)

  # NOTE: if a stride is not always valid, it will be None
  def real_strides(self, ignore_valid=False) -> Tuple[Optional[sint], ...]:
    if len(self.views) == 1 and self.views[-1].mask is None: return self.views[-1].strides
    idxs: List[Node] = [Variable(f"idx{i}", 0, s-1) for i,s in enumerate(self.shape)]
    idx, valid = self.expr_idxs(idxs)
    ret: List[Optional[sint]] = [None] * len(self.views[-1].shape)
    bad_idx_vars: Set[Variable] = set()
    for this_dim in (idx.nodes if isinstance(idx, SumNode) else [idx]):
      idx_maybe, stride_maybe = (this_dim.a, this_dim.b) if isinstance(this_dim, MulNode) else (this_dim, 1)
      try: ret[idxs.index(idx_maybe)] = stride_maybe
      except ValueError: bad_idx_vars = bad_idx_vars.union(idx_maybe.vars())
    idx_vars, valid_vars = idx.vars(), valid.vars()
    for i,tidx in enumerate(idxs):
      if tidx in bad_idx_vars or (tidx in valid_vars and not ignore_valid): ret[i] = None
      elif tidx not in idx_vars: ret[i] = 0
    return tuple(ret)

  def unit_stride_axes(self, ignore_valid=False) -> List[int]: return [i for i,st in enumerate(self.real_strides(ignore_valid)) if st == 1]

  def _expr_idx(self, idx:Node, valid:Node) -> Tuple[Node, Node]:
    for v in reversed(self.views[0:-1]):
      if valid.max == 0: return NumNode(-1), valid
      valid = expr_node_mask(v, idx, valid)
      idx = expr_node(v, idx)
    return idx, valid

  def expr_idxs(self, idxs:Optional[Iterable[Node]]=None):
    if idxs is None: idxs = [Variable(f"idx{i}", 0, s-1) for i,s in enumerate(self.shape)]
    idx = expr_idxs(self.views[-1], tuple(idxs))
    valid = expr_node_mask(self.views[-1], idxs_to_idx(self.views[-1].shape, tuple(idxs)))
    return self._expr_idx(idx, valid)

  def expr_node(self, idx:Union[Node,str]='idx'):
    if isinstance(idx, str): idx = Variable(idx, 0, prod(self.shape)-1)
    return self._expr_idx(expr_node(self.views[-1], idx), expr_node_mask(self.views[-1], idx))

  def axis_is_masked(self, axis:int) -> bool:
    _, valid = self.expr_idxs()
    return f'idx{axis}' in [v.expr for v in valid.vars()]

  def simplify(self) -> ShapeTracker:
    if len(self.views) >= 2 and (new_view := merge_views(self.views[-2], self.views[-1])) is not None:
      return ShapeTracker(self.views[:-2] + (new_view,)).simplify()
    return self

  # *** under this line are the movement ops ***

  def pad(self, arg: Tuple[Tuple[sint, sint], ...]) -> ShapeTracker: return ShapeTracker(self.views[0:-1] + (self.views[-1].pad(arg), ))
  def shrink(self, arg: Tuple[Tuple[sint, sint], ...]) -> ShapeTracker: return ShapeTracker(self.views[0:-1] + (self.views[-1].shrink(arg), ))
  def expand(self, new_shape: Tuple[sint, ...]) -> ShapeTracker: return ShapeTracker(self.views[0:-1] + (self.views[-1].expand(new_shape), ))
  def permute(self, axis: Tuple[int, ...]) -> ShapeTracker: return ShapeTracker(self.views[0:-1] + (self.views[-1].permute(axis), ))
  def stride(self, mul: Tuple[int, ...]) -> ShapeTracker: return ShapeTracker(self.views[0:-1] + (self.views[-1].stride(mul), ))

  def reshape(self, new_shape: Tuple[sint, ...]) -> ShapeTracker:
    if getenv("MERGE_VIEW", 1) and (new_view := self.views[-1].reshape(new_shape)) is not None: return ShapeTracker(self.views[0:-1] + (new_view,))
    return ShapeTracker(self.views + (View.create(new_shape), ))
