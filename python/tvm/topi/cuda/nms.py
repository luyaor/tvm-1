# Licensed to the Apache Software Foundation (ASF) under one
# or more contributor license agreements.  See the NOTICE file
# distributed with this work for additional information
# regarding copyright ownership.  The ASF licenses this file
# to you under the Apache License, Version 2.0 (the
# "License"); you may not use this file except in compliance
# with the License.  You may obtain a copy of the License at
#
#   http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing,
# software distributed under the License is distributed on an
# "AS IS" BASIS, WITHOUT WARRANTIES OR CONDITIONS OF ANY
# KIND, either express or implied.  See the License for the
# specific language governing permissions and limitations
# under the License.
# pylint: disable=invalid-name, no-member, too-many-locals, too-many-arguments, too-many-statements, singleton-comparison
# pylint: disable=bad-continuation, unused-argument
"""Non-maximum suppression operator"""
import tvm
from tvm import te

from tvm.tir import if_then_else
from .sort import argsort, argsort_thrust


def cuda_atomic_add_rule(op):
    if op.dtype == "float32":
        return tvm.tir.call_pure_extern("float32", "atomicAdd", op.args[0], op.args[1])
    if op.dtype == "float64":
        return tvm.tir.call_pure_extern("float64", "atomicAdd", op.args[0], op.args[1])
    if op.dtype == "int32":
        return tvm.tir.call_pure_extern("int32", "atomicAdd", op.args[0], op.args[1])
    raise RuntimeError("only support int32, float32 and float64")


def opencl_atomic_add_rule(op):
    if op.dtype == "int32":
        return tvm.tir.call_pure_extern("int32", "atomic_add", op.args[0], op.args[1])
    raise RuntimeError("only support int32")


tvm.target.intrin.register_intrin_rule("cuda", "atomic_add", cuda_atomic_add_rule, override=True)

tvm.target.intrin.register_intrin_rule(
    "opencl", "atomic_add", opencl_atomic_add_rule, override=True
)


def atomic_add(x, y):
    return tvm.tir.call_intrin(y.dtype, "tir.atomic_add", x, y)


def ceil_div(a, b):
    return tvm.tir.indexdiv(a + b - 1, b)


def get_valid_boxes_ir(data, valid_boxes, score_threshold, id_index, score_index):
    """Low level IR to identify bounding boxes given a score threshold.

    Parameters
    ----------
    data : Buffer
        Input data. 3-D Buffer with shape [batch_size, num_anchors, elem_length].

    score_threshold : Buffer or float32
        Lower limit of score for valid bounding boxes.

    id_index : optional, int
        index of the class categories, -1 to disable.

    score_index: optional, int
        Index of the scores/confidence of boxes.

    Returns
    -------
    valid_boxes: Buffer
        2D Buffer  indicating valid boxes with shape [batch_size, num_anchors].

    """
    batch_size = data.shape[0]
    num_anchors = data.shape[1]
    elem_length = data.shape[2]

    ib = tvm.tir.ir_builder.create()

    data = ib.buffer_ptr(data)

    valid_boxes = ib.buffer_ptr(valid_boxes)
    if isinstance(score_threshold, float):
        score_threshold = tvm.tir.FloatImm("float32", score_threshold)
    id_index = tvm.tir.IntImm("int32", id_index)
    score_index = tvm.tir.IntImm("int32", score_index)

    max_threads = int(tvm.target.Target.current(allow_none=False).max_num_threads)
    with ib.new_scope():
        nthread_tx = max_threads
        nthread_bx = ceil_div(num_anchors, max_threads)
        nthread_by = batch_size
        tx = te.thread_axis("threadIdx.x")
        bx = te.thread_axis("blockIdx.x")
        by = te.thread_axis("blockIdx.y")
        ib.scope_attr(tx, "thread_extent", nthread_tx)
        ib.scope_attr(bx, "thread_extent", nthread_bx)
        ib.scope_attr(by, "thread_extent", nthread_by)
        tid = bx * max_threads + tx

        with ib.if_scope(tid < num_anchors):
            i = by
            j = tid
            score = data[(i * num_anchors + j) * elem_length + score_index]
            with ib.if_scope(
                tvm.tir.all(
                    score > score_threshold,
                    tvm.tir.any(
                        id_index < 0, data[(i * num_anchors + j) * elem_length + id_index] >= 0
                    ),
                )
            ):
                valid_boxes[i * num_anchors + j] = 1
            with ib.else_scope():
                valid_boxes[i * num_anchors + j] = 0
    return ib.get()


def get_valid_indices_ir(valid_boxes, valid_count, valid_indices):
    """Low level IR to get the ouput indices of valid boxes
    and the count of valid boxes

    Parameters
    ----------
    valid_boxes: Buffer
        2D Buffer  indicating valid boxes with shape [batch_size, num_anchors].

    Returns
    -------
    valid_count: Buffer
        1D Buffer of number of valid boxes per batch [batch_size].

    valid_indices: Buffer
        2D Buffer indicating output sorted indcies of valid boxes [batch_size, num_anchors].
    """
    batch_size = valid_boxes.shape[0]
    num_anchors = valid_boxes.shape[1]

    ib = tvm.tir.ir_builder.create()

    valid_boxes = ib.buffer_ptr(valid_boxes)

    valid_count = ib.buffer_ptr(valid_count)
    valid_indices = ib.buffer_ptr(valid_indices)

    max_threads = int(tvm.target.Target.current(allow_none=False).max_num_threads)
    with ib.if_scope(num_anchors > 0):
        # Copy boxes to valid_indices
        with ib.new_scope():
            nthread_tx = max_threads
            nthread_bx = ceil_div(num_anchors, max_threads)
            nthread_by = batch_size
            tx = te.thread_axis("threadIdx.x")
            bx = te.thread_axis("blockIdx.x")
            by = te.thread_axis("blockIdx.y")
            ib.scope_attr(tx, "thread_extent", nthread_tx)
            ib.scope_attr(bx, "thread_extent", nthread_bx)
            ib.scope_attr(by, "thread_extent", nthread_by)
            tid = bx * nthread_tx + tx
            with ib.if_scope(tid < num_anchors):
                valid_indices[by, tid] = valid_boxes[by, tid]

        nthread_tx = max_threads
        nthread_bx = ceil_div(num_anchors, max_threads)
        nthread_by = batch_size

        ## The following algorithm performs parallel exclusive scan to get
        ## a tensor that can later be used to select valid indices
        # Up Sweep of exclusive scan
        lim = tvm.tir.generic.cast(
            tvm.tir.ceil(tvm.tir.log2(tvm.tir.generic.cast(num_anchors, "float64"))), "int64"
        )
        with ib.for_range(0, lim, dtype="int64") as l2_width:
            width = 2 << l2_width

            with ib.new_scope():
                tx = te.thread_axis("threadIdx.x")
                bx = te.thread_axis("blockIdx.x")
                ib.scope_attr(tx, "thread_extent", nthread_tx)
                ib.scope_attr(
                    bx,
                    "thread_extent",
                    tvm.tir.generic.cast(ceil_div(num_anchors, max_threads * width), "int32"),
                )
                tid = bx * nthread_tx + tx

                by = te.thread_axis("blockIdx.y")
                ib.scope_attr(by, "thread_extent", nthread_by)
                start = ib.allocate("int64", (1,), name="start", scope="local")
                middle = ib.allocate("int64", (1,), name="middle", scope="local")
                end = ib.allocate("int64", (1,), name="end", scope="local")
                start[0] = width * tid
                with ib.if_scope(start[0] < num_anchors):
                    middle[0] = start[0] + tvm.tir.indexdiv(width, 2)
                    end[0] = tvm.te.min(start[0] + width, num_anchors)
                    with ib.if_scope(middle[0] < num_anchors):
                        valid_indices[by * num_anchors + end[0] - 1] += valid_indices[
                            by * num_anchors + middle[0] - 1
                        ]

        # Down Sweep of exclusive scan
        with ib.new_scope():
            bx = te.thread_axis("blockIdx.x")
            ib.scope_attr(bx, "thread_extent", batch_size)
            with ib.if_scope(bx < batch_size):
                valid_count[bx] = valid_indices[(bx + 1) * num_anchors - 1]
                valid_indices[(bx + 1) * num_anchors - 1] = 0

        with ib.for_range(0, lim, dtype="int64") as l2_width:
            width = 2 << (lim - l2_width - 1)

            with ib.new_scope():
                tx = te.thread_axis("threadIdx.x")
                bx = te.thread_axis("blockIdx.x")
                ib.scope_attr(tx, "thread_extent", nthread_tx)
                ib.scope_attr(
                    bx,
                    "thread_extent",
                    tvm.tir.generic.cast(ceil_div(num_anchors, max_threads * width), "int32"),
                )
                tid = bx * nthread_tx + tx

                by = te.thread_axis("blockIdx.y")
                ib.scope_attr(by, "thread_extent", nthread_by)
                start = ib.allocate("int64", (1,), name="start", scope="local")
                middle = ib.allocate("int64", (1,), name="middle", scope="local")
                end = ib.allocate("int64", (1,), name="end", scope="local")
                tmp = ib.allocate("int32", (1,), name="end", scope="local")
                start[0] = width * tid
                with ib.if_scope(tvm.tir.all(start[0] < num_anchors)):
                    middle[0] = start[0] + tvm.tir.indexdiv(width, 2)
                    end[0] = tvm.tir.min(start[0] + width, num_anchors)
                    with ib.if_scope(middle[0] < num_anchors):
                        tmp[0] = valid_indices[by * num_anchors + middle[0] - 1]
                        valid_indices[by * num_anchors + middle[0] - 1] = valid_indices[
                            by * num_anchors + end[0] - 1
                        ]
                        valid_indices[by * num_anchors + end[0] - 1] += tmp[0]
    with ib.else_scope():
        with ib.new_scope():
            bx = te.thread_axis("blockIdx.x")
            ib.scope_attr(bx, "thread_extent", batch_size)
            with ib.if_scope(bx < batch_size):
                valid_count[bx] = 0

    return ib.get()


def get_valid_counts_ir(data, valid_indices, valid_boxes, out, out_indices):
    """Low level IR to get valid count of bounding boxes
    given a score threshold. Also prepares to move valid boxes to the
    top of input data.

    Parameters
    ----------
    data : Buffer
        Input data. 3-D Buffer with shape [batch_size, num_anchors, elem_length].

    valid_indices: Buffer
        2D Buffer of flag indicating valid data with shape [batch_size, num_anchors].

    Returns
    -------
    out : Buffer
        Sorted valid boxes

    out_indices : Buffer
        Incidices of valid boxes in original data
    """
    batch_size = data.shape[0]
    num_anchors = data.shape[1]
    elem_length = data.shape[2]

    ib = tvm.tir.ir_builder.create()

    data = ib.buffer_ptr(data)
    valid_indices = ib.buffer_ptr(valid_indices)
    valid_boxes = ib.buffer_ptr(valid_boxes)

    out = ib.buffer_ptr(out)
    out_indices = ib.buffer_ptr(out_indices)
    one = tvm.tir.const(1, dtype=out.dtype)

    max_threads = int(tvm.target.Target.current(allow_none=False).max_num_threads)
    nthread_tx = max_threads
    nthread_bx = num_anchors // max_threads + 1
    nthread_by = batch_size
    with ib.new_scope():
        tx = te.thread_axis("threadIdx.x")
        bx = te.thread_axis("blockIdx.x")
        by = te.thread_axis("blockIdx.y")
        ib.scope_attr(tx, "thread_extent", nthread_tx)
        ib.scope_attr(bx, "thread_extent", nthread_bx)
        ib.scope_attr(by, "thread_extent", nthread_by)
        tid = bx * max_threads + tx
        with ib.if_scope(tid < num_anchors):
            i = by
            j = tid
            with ib.for_range(0, elem_length) as k:
                out[(i * num_anchors + j) * elem_length + k] = -one
            out_indices[i * num_anchors + j] = -1
    with ib.new_scope():
        tx = te.thread_axis("threadIdx.x")
        bx = te.thread_axis("blockIdx.x")
        by = te.thread_axis("blockIdx.y")
        ib.scope_attr(tx, "thread_extent", nthread_tx)
        ib.scope_attr(bx, "thread_extent", nthread_bx)
        ib.scope_attr(by, "thread_extent", nthread_by)
        tid = bx * max_threads + tx
        with ib.if_scope(tid < num_anchors):
            i = by
            j = tid
            with ib.if_scope(valid_boxes[i, tid] > 0):
                with ib.for_range(0, elem_length) as k:
                    out[(i * num_anchors + valid_indices[i, tid]) * elem_length + k] = data[
                        (i * num_anchors + j) * elem_length + k
                    ]
                out_indices[i * num_anchors + valid_indices[i, tid]] = j
    return ib.get()


def get_valid_counts(data, score_threshold=0, id_index=0, score_index=1):
    """Get valid count of bounding boxes given a score threshold.
    Also moves valid boxes to the top of input data.

    Parameters
    ----------
    data : tvm.te.Tensor
        Input data. 3-D tensor with shape [batch_size, num_anchors, elem_length].

    score_threshold : optional, tvm.te.Tensor or float
        Lower limit of score for valid bounding boxes.

    id_index : optional, int
        index of the class categories, -1 to disable.

    score_index: optional, int
        Index of the scores/confidence of boxes.

    Returns
    -------
    valid_count : tvm.te.Tensor
        1-D tensor for valid number of boxes.

    out_tensor : tvm.te.Tensor
        Rearranged data tensor.
    """
    batch_size = data.shape[0]
    num_anchors = data.shape[1]
    data_buf = tvm.tir.decl_buffer(data.shape, data.dtype, "data_buf", data_alignment=8)
    valid_boxes_buf = tvm.tir.decl_buffer(
        (batch_size, num_anchors), "int32", "valid_boxes_buf", data_alignment=8
    )
    valid_boxes = te.extern(
        [(batch_size, num_anchors)],
        [data],
        lambda ins, outs: get_valid_boxes_ir(
            ins[0], outs[0], score_threshold, id_index, score_index
        ),
        dtype=["int32"],
        in_buffers=[data_buf],
        out_buffers=[valid_boxes_buf],
        name="get_valid_boxes",
        tag="get_valid_boxes_gpu",
    )

    valid_indices_buf = tvm.tir.decl_buffer(
        (batch_size, num_anchors), "int32", "valid_indices_buf", data_alignment=8
    )
    valid_count_buf = tvm.tir.decl_buffer(
        (batch_size,), "int32", "valid_count_buf", data_alignment=8
    )
    valid_count, valid_indices = te.extern(
        [(batch_size,), (batch_size, num_anchors)],
        [valid_boxes],
        lambda ins, outs: get_valid_indices_ir(ins[0], outs[0], outs[1]),
        dtype=["int32"],
        in_buffers=[valid_boxes_buf],
        out_buffers=[valid_count_buf, valid_indices_buf],
        name="get_valid_indices",
        tag="get_valid_indices_gpu",
    )

    out_buf = tvm.tir.decl_buffer(data.shape, data.dtype, "out_buf", data_alignment=8)
    out_indices_buf = tvm.tir.decl_buffer(
        (batch_size, num_anchors), "int32", "out_buf", data_alignment=8
    )

    out, out_indices = te.extern(
        [data.shape, (batch_size, num_anchors)],
        [data, valid_indices, valid_boxes],
        lambda ins, outs: get_valid_counts_ir(ins[0], ins[1], ins[2], outs[0], outs[1]),
        dtype=["int32", data.dtype],
        in_buffers=[data_buf, valid_indices_buf, valid_boxes_buf],
        out_buffers=[out_buf, out_indices_buf],
        name="get_valid_counts",
        tag="get_valid_counts_gpu",
    )

    return [valid_count, out, out_indices]


def nms_ir(
    data,
    sorted_index,
    valid_count,
    indices,
    out,
    box_indices,
    num_valid_boxes,
    max_output_size,
    iou_threshold,
    force_suppress,
    top_k,
    coord_start,
    id_index,
    score_index,
    return_indices,
):
    """Low level IR routing for transform location in multibox_detection operator.

    Parameters
    ----------
    data : Buffer
        Buffer of output boxes with class and score.

    sorted_index : Buffer
        Buffer of output box indexes sorted by score.

    valid_count : Buffer
        Buffer of number of valid output boxes.

    indices : Buffer
        indices in original tensor, with shape [batch_size, num_anchors],
        represents the index of box in original data. It could be the third
        output out_indices of get_valid_counts. The values in the second
        dimension are like the output of arange(num_anchors) if get_valid_counts
        is not used before non_max_suppression.

    out : Buffer
        Output buffer, to be filled with sorted boxes.

    box_indices : Buffer
        A indices tensor mapping sorted indices to original indices
        This is the first output of NMS when return_indices=True.

    num_valid_boxes : Buffer
        Record the number of boxes that have survived IOU tests.
        This is the second output of NMS when return_indices=True.

    max_output_size : int
        Max number of output valid boxes for each instance.
        By default all valid boxes are returned.

    iou_threshold : float
        Overlapping(IoU) threshold to suppress object with smaller score.

    force_suppress : boolean
        Whether to suppress all detections regardless of class_id.

    top_k : int
        Keep maximum top k detections before nms, -1 for no limit.

    coord_start : int
        Start index of the consecutive 4 coordinates.

    id_index : int
        index of the class categories, -1 to disable.

    score_index : optional, int
        Index of the scores/confidence of boxes.

    return_indices : boolean
        Whether to return box indices in input data.

    Returns
    -------
    stmt : Stmt
        The result IR statement.
    """

    def get_boundaries(output, box_idx):
        l = tvm.te.min(
            output[box_idx],
            output[box_idx + 2],
        )
        t = tvm.te.min(
            output[box_idx + 1],
            output[box_idx + 3],
        )
        r = tvm.te.max(
            output[box_idx],
            output[box_idx + 2],
        )
        b = tvm.te.max(
            output[box_idx + 1],
            output[box_idx + 3],
        )
        return l, t, r, b

    def calculate_overlap(out_tensor, box_a_idx, box_b_idx):
        """Calculate overlap of two boxes."""
        a_l, a_t, a_r, a_b = get_boundaries(out_tensor, box_a_idx)
        b_l, b_t, b_r, b_b = get_boundaries(out_tensor, box_b_idx)

        # Overlapping width and height
        w = tvm.te.max(0.0, tvm.te.min(a_r, b_r) - tvm.te.max(a_l, b_l))
        h = tvm.te.max(0.0, tvm.te.min(a_b, b_b) - tvm.te.max(a_t, b_t))

        # Overlapping area
        area = h * w

        # total area of the figure formed by box a and box b
        # except for overlapping area
        u = (a_r - a_l) * (a_b - a_t) + (b_r - b_l) * (b_b - b_t) - area
        return tvm.tir.Select(u <= 0.0, 0.0, area / u)

    batch_size = data.shape[0]
    num_anchors = data.shape[1]
    box_data_length = data.shape[2]

    ib = tvm.tir.ir_builder.create()

    data = ib.buffer_ptr(data)
    sorted_index = ib.buffer_ptr(sorted_index)
    valid_count = ib.buffer_ptr(valid_count)
    indices = ib.buffer_ptr(indices)
    num_valid_boxes = ib.buffer_ptr(num_valid_boxes)
    out = ib.buffer_ptr(out)
    box_indices = ib.buffer_ptr(box_indices)

    if isinstance(iou_threshold, float):
        iou_threshold = tvm.tir.FloatImm("float32", iou_threshold)
    top_k = tvm.tir.IntImm("int32", top_k)
    coord_start = tvm.tir.IntImm("int32", coord_start)
    id_index = tvm.tir.IntImm("int32", id_index)
    score_index = tvm.tir.IntImm("int32", score_index)
    force_suppress = tvm.tir.IntImm("int32", 1 if force_suppress else 0)

    max_threads = int(tvm.target.Target.current(allow_none=False).max_num_threads)

    with ib.new_scope():
        nthread_tx = max_threads
        nthread_bx = ceil_div(num_anchors, max_threads)
        nthread_by = batch_size
        tx = te.thread_axis("threadIdx.x")
        bx = te.thread_axis("blockIdx.x")
        by = te.thread_axis("blockIdx.y")
        ib.scope_attr(by, "thread_extent", nthread_by)
        ib.scope_attr(tx, "thread_extent", nthread_tx)
        ib.scope_attr(bx, "thread_extent", nthread_bx)
        i = by
        base_idx = i * num_anchors * box_data_length
        with ib.if_scope(tvm.tir.all(iou_threshold > 0, valid_count[i] > 0)):
            # Reorder output
            nkeep = if_then_else(
                tvm.tir.all(top_k > 0, top_k < valid_count[i]), top_k, valid_count[i]
            )
            j = bx * max_threads + tx
            with ib.if_scope(j < num_anchors):
                box_indices[i * num_anchors + j] = -1
            with ib.if_scope(j < nkeep):
                # Fill in out with sorted boxes
                with ib.for_range(0, box_data_length) as k:
                    out[(base_idx + j * box_data_length + k)] = data[
                        (base_idx + sorted_index[i * num_anchors + j] * box_data_length + k)
                    ]
            with ib.else_scope():
                # Indices > nkeep are discarded
                with ib.if_scope(j < num_anchors):
                    with ib.for_range(0, box_data_length) as k:
                        out[(base_idx + j * box_data_length + k)] = -1.0
        with ib.else_scope():
            with ib.if_scope(j < valid_count[i]):
                with ib.for_range(0, box_data_length) as k:
                    offset = base_idx + j * box_data_length + k
                    out[offset] = data[offset]
                box_indices[i * num_anchors + j] = j

    with ib.new_scope():
        nthread_by = batch_size
        nthread_tx = max_threads

        by = te.thread_axis("blockIdx.y")
        tx = te.thread_axis("threadIdx.x")
        ib.scope_attr(by, "thread_extent", nthread_by)
        ib.scope_attr(tx, "thread_extent", nthread_tx)

        i = by

        base_idx = i * num_anchors * box_data_length
        num_valid_boxes_local = ib.allocate(
            "int32", (1,), name="num_valid_boxes_local", scope="local"
        )
        num_valid_boxes_local[0] = 0
        nkeep = if_then_else(tvm.tir.all(top_k > 0, top_k < valid_count[i]), top_k, valid_count[i])

        def nms_inner_loop(ib, j):
            # The box j is valid, invalidate other boxes that overlap with j above iou_threshold

            # When return_indices is False, no need to populate box_indices
            if return_indices:
                with ib.if_scope(tx + 0 == 0):
                    orig_idx = sorted_index[i * num_anchors + j]
                    box_indices[i, num_valid_boxes_local[0]] = indices[i, orig_idx]

            num_valid_boxes_local[0] += 1

            offset_j = j * box_data_length
            num_iter_per_thread = ceil_div(nkeep - (j + 1), nthread_tx)

            with ib.for_range(0, num_iter_per_thread) as _k:
                k = j + 1 + _k * nthread_tx + tx
                offset_k = k * box_data_length

                with ib.if_scope(
                    tvm.tir.all(
                        k < nkeep,
                        out[base_idx + offset_k + score_index] > 0,  # is the box k still valid?
                        tvm.tir.any(
                            force_suppress > 0,
                            id_index < 0,
                            out[base_idx + offset_k + id_index]
                            == out[base_idx + offset_j + id_index],
                        ),
                    )
                ):
                    iou = calculate_overlap(
                        out,
                        base_idx + offset_j + coord_start,
                        base_idx + offset_k + coord_start,
                    )
                    with ib.if_scope(iou >= iou_threshold):
                        # invalidate the box k
                        out[base_idx + offset_k + score_index] = -1.0
                        with ib.if_scope(id_index >= 0):
                            out[base_idx + offset_k + id_index] = -1.0

                # Make sure to do the next loop in a lock step
                ib.emit(tvm.tir.Call(None, "tir.tvm_storage_sync", tvm.runtime.convert(["shared"])))

        if isinstance(max_output_size, int):
            max_output_size = tvm.tir.const(max_output_size)

        with ib.if_scope(tvm.tir.all(iou_threshold > 0, valid_count[i] > 0)):
            # Apply nms
            with ib.for_range(0, nkeep) as j:
                # Proceed to the inner loop if the box j is still valid
                with ib.if_scope(out[base_idx + (j * box_data_length) + score_index] > -1.0):
                    with ib.if_scope(max_output_size > 0):
                        # No need to do more iteration if we already reach max_output_size boxes
                        with ib.if_scope(num_valid_boxes_local[0] < max_output_size):
                            nms_inner_loop(ib, j)
                    with ib.else_scope():
                        nms_inner_loop(ib, j)

            with ib.if_scope(tx + 0 == 0):
                num_valid_boxes[i] = num_valid_boxes_local[0]

        with ib.else_scope():
            num_valid_boxes[i] = 0

    return ib.get()


def _fetch_score_ir(data, score, axis):
    """
    Fetch score from data.
    This routine is required for dynamic shape nms.
    """
    batch_size = data.shape[0]
    num_anchors = data.shape[1]
    elem_length = data.shape[2]

    ib = tvm.tir.ir_builder.create()

    data = ib.buffer_ptr(data)
    score = ib.buffer_ptr(score)
    with ib.if_scope(num_anchors > 0):
        max_threads = int(tvm.target.Target.current(allow_none=False).max_num_threads)
        nthread_tx = max_threads
        nthread_bx = batch_size * num_anchors // max_threads + 1
        tx = te.thread_axis("threadIdx.x")
        bx = te.thread_axis("blockIdx.x")
        ib.scope_attr(tx, "thread_extent", nthread_tx)
        ib.scope_attr(bx, "thread_extent", nthread_bx)

        tid = bx * max_threads + tx
        with ib.if_scope(tid < batch_size * num_anchors):
            score[tid] = data[tid * elem_length + axis]

    return ib.get()


def non_max_suppression(
    data,
    valid_count,
    indices,
    max_output_size=-1,
    iou_threshold=0.5,
    force_suppress=False,
    top_k=-1,
    coord_start=2,
    score_index=1,
    id_index=0,
    return_indices=True,
    invalid_to_bottom=False,
):
    """Non-maximum suppression operator for object detection.

    Parameters
    ----------
    data : tvm.te.Tensor
        3-D tensor with shape [batch_size, num_anchors, elem_length].
        The last dimension should be in format of
        [class_id, score, box_left, box_top, box_right, box_bottom].
        It could be the second output out_tensor of get_valid_counts.

    valid_count : tvm.te.Tensor
        1-D tensor for valid number of boxes. It could be the output
        valid_count of get_valid_counts.

    indices : tvm.te.Tensor
        2-D tensor with shape [batch_size, num_anchors], represents
        the index of box in original data. It could be the third
        output out_indices of get_valid_counts. The values in the
        second dimension are like the output of arange(num_anchors)
        if get_valid_counts is not used before non_max_suppression.

    max_output_size : optional, tvm.te.Tensor or int
        Max number of output valid boxes for each instance.
        By default all valid boxes are returned.

    iou_threshold : optional, tvm.te.Tensor or float
        Non-maximum suppression threshold.

    force_suppress : optional, boolean
        Whether to suppress all detections regardless of class_id.

    top_k : optional, int
        Keep maximum top k detections before nms, -1 for no limit.

    coord_start : required, int
        Start index of the consecutive 4 coordinates.

    score_index : optional, int
        Index of the scores/confidence of boxes.

    id_index : optional, int
        index of the class categories, -1 to disable.

    return_indices : boolean
        Whether to return box indices in input data.

    invalid_to_bottom : optional, boolean
        Whether to move all valid bounding boxes to the top.

    Returns
    -------
    out : tvm.te.Tensor
        3-D tensor with shape [batch_size, num_anchors, elem_length].

    Example
    --------
    .. code-block:: python

        # An example to use nms
        dshape = (1, 5, 6)
        data = te.placeholder(dshape, name="data")
        valid_count = te.placeholder((dshape[0],), dtype="int32", name="valid_count")
        iou_threshold = 0.7
        force_suppress = True
        top_k = -1
        out = non_max_suppression(data=data, valid_count=valid_count, iou_threshold=iou_threshold,
                                 force_suppress=force_supress, top_k=top_k, return_indices=False)
        np_data = np.random.uniform(dshape)
        np_valid_count = np.array([4])
        s = topi.generic.schedule_nms(out)
        f = tvm.build(s, [data, valid_count, out], "cuda")
        ctx = tvm.gpu(0)
        tvm_data = tvm.nd.array(np_data, ctx)
        tvm_valid_count = tvm.nd.array(np_valid_count, ctx)
        tvm_out = tvm.nd.array(np.zeros(dshape, dtype=data.dtype), ctx)
        f(tvm_data, tvm_valid_count, tvm_out)
    """
    batch_size = data.shape[0]
    num_anchors = data.shape[1]

    valid_count_dtype = "int32"
    valid_count_buf = tvm.tir.decl_buffer(
        valid_count.shape, valid_count_dtype, "valid_count_buf", data_alignment=4
    )
    score_axis = score_index
    score_shape = (batch_size, num_anchors)
    data_buf = tvm.tir.decl_buffer(data.shape, data.dtype, "data_buf", data_alignment=8)
    score_buf = tvm.tir.decl_buffer(score_shape, data.dtype, "score_buf", data_alignment=8)
    score_tensor = te.extern(
        [score_shape],
        [data],
        lambda ins, outs: _fetch_score_ir(
            ins[0],
            outs[0],
            score_axis,
        ),
        dtype=[data.dtype],
        in_buffers=[data_buf],
        out_buffers=[score_buf],
        name="fetch_score",
        tag="fetch_score",
    )
    target = tvm.target.Target.current()
    if (
        target
        and target.kind.name == "cuda"
        and tvm.get_global_func("tvm.contrib.thrust.sort_nms", allow_missing=True)
    ):
        sort_tensor = argsort_thrust(
            score_tensor, valid_count=None, axis=1, is_ascend=False, dtype=valid_count_dtype
        )
    else:
        sort_tensor = argsort(score_tensor, axis=1, is_ascend=False, dtype=valid_count_dtype)

    sort_tensor_buf = tvm.tir.decl_buffer(
        sort_tensor.shape, sort_tensor.dtype, "sort_tensor_buf", data_alignment=8
    )

    data_buf = tvm.tir.decl_buffer(data.shape, data.dtype, "data_buf", data_alignment=8)
    indices_buf = tvm.tir.decl_buffer(indices.shape, indices.dtype, "indices_buf", data_alignment=8)

    out, box_indices, num_valid_boxes = te.extern(
        [data.shape, score_shape, [batch_size, 1]],
        [data, sort_tensor, valid_count, indices],
        lambda ins, outs: nms_ir(
            ins[0],
            ins[1],
            ins[2],
            ins[3],
            outs[0],
            outs[1],
            outs[2],
            max_output_size,
            iou_threshold,
            force_suppress,
            top_k,
            coord_start,
            id_index,
            score_index,
            return_indices,
        ),
        dtype=[data.dtype, "int32", "int32"],
        in_buffers=[data_buf, sort_tensor_buf, valid_count_buf, indices_buf],
        name="nms",
        tag="nms",
    )

    if return_indices:
        return [box_indices, num_valid_boxes]

    return out
