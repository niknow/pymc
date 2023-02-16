#   Copyright 2023 The PyMC Developers
#
#   Licensed under the Apache License, Version 2.0 (the "License");
#   you may not use this file except in compliance with the License.
#   You may obtain a copy of the License at
#
#       http://www.apache.org/licenses/LICENSE-2.0
#
#   Unless required by applicable law or agreed to in writing, software
#   distributed under the License is distributed on an "AS IS" BASIS,
#   WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
#   See the License for the specific language governing permissions and
#   limitations under the License.
#
#   MIT License
#
#   Copyright (c) 2021-2022 aesara-devs
#
#   Permission is hereby granted, free of charge, to any person obtaining a copy
#   of this software and associated documentation files (the "Software"), to deal
#   in the Software without restriction, including without limitation the rights
#   to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
#   copies of the Software, and to permit persons to whom the Software is
#   furnished to do so, subject to the following conditions:
#
#   The above copyright notice and this permission notice shall be included in all
#   copies or substantial portions of the Software.
#
#   THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
#   IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
#   FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
#   AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
#   LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
#   OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
#   SOFTWARE.

from typing import List, Optional, Tuple, Union, cast

import pytensor
import pytensor.tensor as pt

from pytensor.graph.basic import Apply, Constant, Variable
from pytensor.graph.fg import FunctionGraph
from pytensor.graph.op import Op, compute_test_value
from pytensor.graph.rewriting.basic import (
    EquilibriumGraphRewriter,
    node_rewriter,
    pre_greedy_node_rewriter,
)
from pytensor.ifelse import ifelse
from pytensor.scalar.basic import Switch
from pytensor.tensor.basic import Join, MakeVector
from pytensor.tensor.elemwise import Elemwise
from pytensor.tensor.random.rewriting import (
    local_dimshuffle_rv_lift,
    local_rv_size_lift,
    local_subtensor_rv_lift,
)
from pytensor.tensor.shape import shape_tuple
from pytensor.tensor.subtensor import (
    AdvancedSubtensor,
    AdvancedSubtensor1,
    as_index_literal,
    as_nontensor_scalar,
    get_canonical_form_slice,
    is_basic_idx,
)
from pytensor.tensor.type import TensorType
from pytensor.tensor.type_other import NoneConst, NoneTypeT, SliceConstant, SliceType
from pytensor.tensor.var import TensorVariable

from pymc.logprob.abstract import MeasurableVariable, _logprob, logprob
from pymc.logprob.rewriting import (
    local_lift_DiracDelta,
    logprob_rewrites_db,
    subtensor_ops,
)
from pymc.logprob.tensor import naive_bcast_rv_lift
from pymc.logprob.utils import ignore_logprob


def is_newaxis(x):
    return isinstance(x, type(None)) or isinstance(getattr(x, "type", None), NoneTypeT)


def expand_indices(
    indices: Tuple[Optional[Union[Variable, slice]], ...], shape: Tuple[TensorVariable]
) -> Tuple[TensorVariable]:
    """Convert basic and/or advanced indices into a single, broadcasted advanced indexing operation.

    Parameters
    ----------
    indices
        The indices to convert.
    shape
        The shape of the array being indexed.

    """
    n_non_newaxis = sum(1 for idx in indices if not is_newaxis(idx))
    n_missing_dims = len(shape) - n_non_newaxis
    full_indices = list(indices) + [slice(None)] * n_missing_dims

    # We need to know if a "subspace" was generated by advanced indices
    # bookending basic indices.  If so, we move the advanced indexing subspace
    # to the "front" of the shape (i.e. left-most indices/last-most
    # dimensions).
    index_types = [is_basic_idx(idx) for idx in full_indices]

    first_adv_idx = len(shape)
    try:
        first_adv_idx = index_types.index(False)
        first_bsc_after_adv_idx = index_types.index(True, first_adv_idx)
        index_types.index(False, first_bsc_after_adv_idx)
        moved_subspace = True
    except ValueError:
        moved_subspace = False

    n_basic_indices = sum(index_types)

    # The number of dimensions in the subspace created by the advanced indices
    n_subspace_dims = max(
        (
            getattr(idx, "ndim", 0)
            for idx, is_basic in zip(full_indices, index_types)
            if not is_basic
        ),
        default=0,
    )

    # The number of dimensions for each expanded index
    n_output_dims = n_subspace_dims + n_basic_indices

    adv_indices = []
    shape_copy = list(shape)
    n_preceding_basics = 0
    for d, idx in enumerate(full_indices):
        if not is_basic_idx(idx):
            s = shape_copy.pop(0)

            idx = pt.as_tensor(idx)

            if moved_subspace:
                # The subspace generated by advanced indices appear as the
                # upper dimensions in the "expanded" index space, so we need to
                # add broadcast dimensions for the non-basic indices to the end
                # of these advanced indices
                expanded_idx = idx[(Ellipsis,) + (None,) * n_basic_indices]
            else:
                # In this case, we need to add broadcast dimensions for the
                # basic indices that proceed and follow the group of advanced
                # indices; otherwise, a contiguous group of advanced indices
                # forms a broadcasted set of indices that are iterated over
                # within the same subspace, which means that all their
                # corresponding "expanded" indices have exactly the same shape.
                expanded_idx = idx[(None,) * n_preceding_basics][
                    (Ellipsis,) + (None,) * (n_basic_indices - n_preceding_basics)
                ]
        else:
            if is_newaxis(idx):
                n_preceding_basics += 1
                continue

            s = shape_copy.pop(0)

            if isinstance(idx, slice) or isinstance(getattr(idx, "type", None), SliceType):
                idx = as_index_literal(idx)
                idx_slice, _ = get_canonical_form_slice(idx, s)
                idx = pt.arange(idx_slice.start, idx_slice.stop, idx_slice.step)

            if moved_subspace:
                # Basic indices appear in the lower dimensions
                # (i.e. right-most) in the output, and are preceded by
                # the subspace generated by the advanced indices.
                expanded_idx = idx[(None,) * (n_subspace_dims + n_preceding_basics)][
                    (Ellipsis,) + (None,) * (n_basic_indices - n_preceding_basics - 1)
                ]
            else:
                # In this case, we need to know when the basic indices have
                # moved past the contiguous group of advanced indices (in the
                # "expanded" index space), so that we can properly pad those
                # dimensions in this basic index's shape.
                # Don't forget that a single advanced index can introduce an
                # arbitrary number of dimensions to the expanded index space.

                # If we're currently at a basic index that's past the first
                # advanced index, then we're necessarily past the group of
                # advanced indices.
                n_preceding_dims = (
                    n_subspace_dims if d > first_adv_idx else 0
                ) + n_preceding_basics
                expanded_idx = idx[(None,) * n_preceding_dims][
                    (Ellipsis,) + (None,) * (n_output_dims - n_preceding_dims - 1)
                ]

            n_preceding_basics += 1

        assert expanded_idx.ndim <= n_output_dims

        adv_indices.append(expanded_idx)

    return cast(Tuple[TensorVariable], tuple(pt.broadcast_arrays(*adv_indices)))


def rv_pull_down(x: TensorVariable, dont_touch_vars=None) -> TensorVariable:
    """Pull a ``RandomVariable`` ``Op`` down through a graph, when possible."""
    fgraph = FunctionGraph(outputs=dont_touch_vars or [], clone=False)

    return pre_greedy_node_rewriter(
        fgraph,
        [
            local_rv_size_lift,
            local_dimshuffle_rv_lift,
            local_subtensor_rv_lift,
            naive_bcast_rv_lift,
            local_lift_DiracDelta,
        ],
        x,
    )


class MixtureRV(Op):
    """A placeholder used to specify a log-likelihood for a mixture sub-graph."""

    __props__ = ("indices_end_idx", "out_dtype", "out_broadcastable")

    def __init__(self, indices_end_idx, out_dtype, out_broadcastable):
        super().__init__()
        self.indices_end_idx = indices_end_idx
        self.out_dtype = out_dtype
        self.out_broadcastable = out_broadcastable

    def make_node(self, *inputs):
        return Apply(self, list(inputs), [TensorType(self.out_dtype, self.out_broadcastable)()])

    def perform(self, node, inputs, outputs):
        raise NotImplementedError("This is a stand-in Op.")  # pragma: no cover


MeasurableVariable.register(MixtureRV)


def get_stack_mixture_vars(
    node: Apply,
) -> Tuple[Optional[List[TensorVariable]], Optional[int]]:
    r"""Extract the mixture terms from a `*Subtensor*` applied to stacked `MeasurableVariable`\s."""

    assert isinstance(node.op, subtensor_ops)

    joined_rvs = node.inputs[0]

    # First, make sure that it's some sort of concatenation
    if not (joined_rvs.owner and isinstance(joined_rvs.owner.op, (MakeVector, Join))):
        return None, None

    if isinstance(joined_rvs.owner.op, MakeVector):
        join_axis = NoneConst
        mixture_rvs = joined_rvs.owner.inputs

    elif isinstance(joined_rvs.owner.op, Join):
        # TODO: Find better solution to avoid this circular dependency
        from pymc.pytensorf import constant_fold

        join_axis = joined_rvs.owner.inputs[0]
        # TODO: Support symbolic join axes. This will raise ValueError if it's not a constant
        (join_axis,) = constant_fold((join_axis,), raise_not_constant=False)
        join_axis = pt.as_tensor(join_axis, dtype="int64")

        mixture_rvs = joined_rvs.owner.inputs[1:]

    return mixture_rvs, join_axis


@node_rewriter(subtensor_ops)
def mixture_replace(fgraph, node):
    r"""Identify mixture sub-graphs and replace them with a place-holder `Op`.

    The basic idea is to find ``stack(mixture_comps)[I_rv]``, where
    ``mixture_comps`` is a ``list`` of `MeasurableVariable`\s and ``I_rv`` is a
    `MeasurableVariable` with a discrete and finite support.
    From these terms, new terms ``Z_rv[i] = mixture_comps[i][i == I_rv]`` are
    created for each ``i`` in ``enumerate(mixture_comps)``.
    """
    rv_map_feature = getattr(fgraph, "preserve_rv_mappings", None)

    if rv_map_feature is None:
        return None  # pragma: no cover

    old_mixture_rv = node.default_output()

    mixture_rvs, join_axis = get_stack_mixture_vars(node)

    # We don't support symbolic join axis
    if mixture_rvs is None or not isinstance(join_axis, (NoneTypeT, Constant)):
        return None

    # Check that all components are MeasurableVariables and none is already conditioned on
    if not all(
        (
            rv.owner is not None
            and isinstance(rv.owner.op, MeasurableVariable)
            and rv not in rv_map_feature.rv_values
        )
        for rv in mixture_rvs
    ):
        return None  # pragma: no cover

    mixing_indices = node.inputs[1:]

    # TODO: Add check / test case for Advanced Boolean indexing
    if isinstance(node.op, (AdvancedSubtensor, AdvancedSubtensor1)):
        # We don't support (non-scalar) integer array indexing as it can pick repeated values,
        # but the Mixture logprob assumes all mixture values are independent
        if any(
            indices.dtype.startswith("int") and sum(1 - b for b in indices.type.broadcastable) > 0
            for indices in mixing_indices
            if not isinstance(indices, SliceConstant)
        ):
            return None

    # We loop through mixture components and collect all the array elements
    # that belong to each one (by way of their indices).
    new_mixture_rvs = []
    for i, component_rv in enumerate(mixture_rvs):
        # We create custom types for the mixture components and assign them
        # null `get_measurable_outputs` dispatches so that they aren't
        # erroneously encountered in places like `factorized_joint_logprob`.
        new_comp_rv = ignore_logprob(component_rv)
        new_mixture_rvs.append(new_comp_rv)

    # Replace this sub-graph with a `MixtureRV`
    mix_op = MixtureRV(
        1 + len(mixing_indices),
        old_mixture_rv.dtype,
        old_mixture_rv.broadcastable,
    )
    new_node = mix_op.make_node(*([join_axis] + mixing_indices + new_mixture_rvs))

    new_mixture_rv = new_node.default_output()

    if pytensor.config.compute_test_value != "off":
        # We can't use `MixtureRV` to compute a test value; instead, we'll use
        # the original node's test value.
        if not hasattr(old_mixture_rv.tag, "test_value"):
            compute_test_value(node)

        new_mixture_rv.tag.test_value = old_mixture_rv.tag.test_value

    if old_mixture_rv.name:
        new_mixture_rv.name = f"{old_mixture_rv.name}-mixture"

    return [new_mixture_rv]


@node_rewriter((Elemwise,))
def switch_mixture_replace(fgraph, node):
    rv_map_feature = getattr(fgraph, "preserve_rv_mappings", None)

    if rv_map_feature is None:
        return None  # pragma: no cover

    if not isinstance(node.op.scalar_op, Switch):
        return None  # pragma: no cover

    old_mixture_rv = node.default_output()
    # idx, component_1, component_2 = node.inputs

    mixture_rvs = []

    for component_rv in node.inputs[1:]:
        if not (
            component_rv.owner
            and isinstance(component_rv.owner.op, MeasurableVariable)
            and component_rv not in rv_map_feature.rv_values
        ):
            return None
        new_comp_rv = ignore_logprob(component_rv)
        mixture_rvs.append(new_comp_rv)

    mix_op = MixtureRV(
        2,
        old_mixture_rv.dtype,
        old_mixture_rv.broadcastable,
    )
    new_node = mix_op.make_node(*([NoneConst, as_nontensor_scalar(node.inputs[0])] + mixture_rvs))

    new_mixture_rv = new_node.default_output()

    if pytensor.config.compute_test_value != "off":
        if not hasattr(old_mixture_rv.tag, "test_value"):
            compute_test_value(node)

        new_mixture_rv.tag.test_value = old_mixture_rv.tag.test_value

    if old_mixture_rv.name:
        new_mixture_rv.name = f"{old_mixture_rv.name}-mixture"

    return [new_mixture_rv]


@_logprob.register(MixtureRV)
def logprob_MixtureRV(
    op, values, *inputs: Optional[Union[TensorVariable, slice]], name=None, **kwargs
):
    (value,) = values

    join_axis = cast(Variable, inputs[0])
    indices = cast(TensorVariable, inputs[1 : op.indices_end_idx])
    comp_rvs = cast(TensorVariable, inputs[op.indices_end_idx :])

    assert len(indices) > 0

    if len(indices) > 1 or indices[0].ndim > 0:
        if isinstance(join_axis.type, NoneTypeT):
            # `join_axis` will be `NoneConst` if the "join" was a `MakeVector`
            # (i.e. scalar measurable variables were combined to make a
            # vector).
            # Since some form of advanced indexing is necessarily occurring, we
            # need to reformat the MakeVector arguments so that they fit the
            # `Join` format expected by the logic below.
            join_axis_val = 0
            comp_rvs = [comp[None] for comp in comp_rvs]
            original_shape = (len(comp_rvs),)
        else:
            # TODO: Find better solution to avoid this circular dependency
            from pymc.pytensorf import constant_fold

            join_axis_val = constant_fold((join_axis,))[0].item()
            original_shape = shape_tuple(comp_rvs[0])

        bcast_indices = expand_indices(indices, original_shape)

        logp_val = pt.empty(bcast_indices[0].shape)

        for m, rv in enumerate(comp_rvs):
            idx_m_on_axis = pt.nonzero(pt.eq(bcast_indices[join_axis_val], m))
            m_indices = tuple(
                v[idx_m_on_axis] for i, v in enumerate(bcast_indices) if i != join_axis_val
            )
            # Drop superfluous join dimension
            rv = rv[0]
            # TODO: Do we really need to do this now?
            # Could we construct this form earlier and
            # do the lifting for everything at once, instead of
            # this intentional one-off?
            rv_m = rv_pull_down(rv[m_indices] if m_indices else rv)
            val_m = value[idx_m_on_axis]
            logp_m = logprob(rv_m, val_m)
            logp_val = pt.set_subtensor(logp_val[idx_m_on_axis], logp_m)

    else:
        # FIXME: This logprob implementation does not support mixing across distinct components,
        # but we sometimes use it, because MixtureRV does not keep information about at which
        # dimension scalar indexing actually starts

        # If the stacking operation expands the component RVs, we have
        # to expand the value and later squeeze the logprob for everything
        # to work correctly
        join_axis_val = None if isinstance(join_axis.type, NoneTypeT) else join_axis.data

        if join_axis_val is not None:
            value = pt.expand_dims(value, axis=join_axis_val)

        logp_val = 0.0
        for i, comp_rv in enumerate(comp_rvs):
            comp_logp = logprob(comp_rv, value)
            if join_axis_val is not None:
                comp_logp = pt.squeeze(comp_logp, axis=join_axis_val)
            logp_val += ifelse(
                pt.eq(indices[0], i),
                comp_logp,
                pt.zeros_like(comp_logp),
            )

    return logp_val


logprob_rewrites_db.register(
    "mixture_replace",
    EquilibriumGraphRewriter(
        [mixture_replace, switch_mixture_replace],
        max_use_ratio=pytensor.config.optdb__max_use_ratio,
    ),
    "basic",
    "mixture",
)
