# Copyright (c) Meta Platforms, Inc. and affiliates.
#
# This source code is licensed under the MIT license found in the
# LICENSE file in the root directory of this source tree.

import argparse
import importlib
from functools import partial
from unittest import mock

import numpy as np
import pytest
import torch
from _utils_internal import get_available_devices
from tensordict.tensordict import assert_allclose_td, TensorDict, TensorDictBase
from torchrl.data import PrioritizedReplayBuffer, ReplayBuffer, TensorDictReplayBuffer
from torchrl.data.replay_buffers import (
    rb_prototype,
    samplers,
    TensorDictPrioritizedReplayBuffer,
    writers,
)
from torchrl.data.replay_buffers.samplers import (
    PrioritizedSampler,
    RandomSampler,
    SamplerWithoutReplacement,
)
from torchrl.data.replay_buffers.storages import (
    LazyMemmapStorage,
    LazyTensorStorage,
    ListStorage,
)
from torchrl.data.replay_buffers.writers import RoundRobinWriter
from torchrl.envs.transforms.transforms import (
    BinarizeReward,
    CatFrames,
    CatTensors,
    CenterCrop,
    DiscreteActionProjection,
    DoubleToFloat,
    FiniteTensorDictCheck,
    FlattenObservation,
    GrayScale,
    gSDENoise,
    ObservationNorm,
    PinMemoryTransform,
    Resize,
    RewardClipping,
    RewardScaling,
    SqueezeTransform,
    ToTensorImage,
    UnsqueezeTransform,
    VecNorm,
)

_has_tv = importlib.util.find_spec("torchvision") is not None


@pytest.mark.parametrize(
    "rb_type",
    [
        rb_prototype.ReplayBuffer,
        rb_prototype.TensorDictReplayBuffer,
        rb_prototype.RemoteTensorDictReplayBuffer,
    ],
)
@pytest.mark.parametrize(
    "sampler", [samplers.RandomSampler, samplers.PrioritizedSampler]
)
@pytest.mark.parametrize("writer", [writers.RoundRobinWriter])
@pytest.mark.parametrize("storage", [ListStorage, LazyTensorStorage, LazyMemmapStorage])
@pytest.mark.parametrize("size", [3, 5, 100])
class TestPrototypeBuffers:
    def _get_rb(self, rb_type, size, sampler, writer, storage):

        if storage is not None:
            storage = storage(size)

        sampler_args = {}
        if sampler is samplers.PrioritizedSampler:
            sampler_args = {"max_capacity": size, "alpha": 0.8, "beta": 0.9}

        sampler = sampler(**sampler_args)
        writer = writer()
        rb = rb_type(storage=storage, sampler=sampler, writer=writer)
        return rb

    def _get_datum(self, rb_type):
        if rb_type is rb_prototype.ReplayBuffer:
            data = torch.randint(100, (1,))
        elif (
            rb_type is rb_prototype.TensorDictReplayBuffer
            or rb_type is rb_prototype.RemoteTensorDictReplayBuffer
        ):
            data = TensorDict({"a": torch.randint(100, (1,))}, [])
        else:
            raise NotImplementedError(rb_type)
        return data

    def _get_data(self, rb_type, size):
        if rb_type is rb_prototype.ReplayBuffer:
            data = torch.randint(100, (size, 1))
        elif (
            rb_type is rb_prototype.TensorDictReplayBuffer
            or rb_type is rb_prototype.RemoteTensorDictReplayBuffer
        ):
            data = TensorDict(
                {
                    "a": torch.randint(100, (size,)),
                    "b": TensorDict({"c": torch.randint(100, (size,))}, [size]),
                },
                [size],
            )
        else:
            raise NotImplementedError(rb_type)
        return data

    def test_add(self, rb_type, sampler, writer, storage, size):
        torch.manual_seed(0)
        rb = self._get_rb(
            rb_type=rb_type, sampler=sampler, writer=writer, storage=storage, size=size
        )
        data = self._get_datum(rb_type)
        rb.add(data)
        s = rb._storage[0]
        if isinstance(s, TensorDictBase):
            assert (s == data.select(*s.keys())).all()
        else:
            assert (s == data).all()

    def test_cursor_position(self, rb_type, sampler, writer, storage, size):
        storage = storage(size)
        writer = writer()
        writer.register_storage(storage)
        batch1 = self._get_data(rb_type, size=5)
        writer.extend(batch1)

        # Added less data than storage max size
        if size > 5:
            assert writer._cursor == 5
        # Added more data than storage max size
        elif size < 5:
            assert writer._cursor == 5 - size
        # Added as data as storage max size
        else:
            assert writer._cursor == 0
            batch2 = self._get_data(rb_type, size=size - 1)
            writer.extend(batch2)
            assert writer._cursor == size - 1

    def test_extend(self, rb_type, sampler, writer, storage, size):
        torch.manual_seed(0)
        rb = self._get_rb(
            rb_type=rb_type, sampler=sampler, writer=writer, storage=storage, size=size
        )
        data = self._get_data(rb_type, size=5)
        rb.extend(data)
        length = len(rb)
        for d in data[-length:]:
            found_similar = False
            for b in rb._storage:
                if isinstance(b, TensorDictBase):
                    keys = set(d.keys()).intersection(b.keys())
                    b = b.exclude("index").select(*keys, strict=False)
                    keys = set(d.keys()).intersection(b.keys())
                    d = d.select(*keys, strict=False)

                value = b == d
                if isinstance(value, (torch.Tensor, TensorDictBase)):
                    value = value.all()
                if value:
                    break
            else:
                raise RuntimeError("did not find match")

    def test_sample(self, rb_type, sampler, writer, storage, size):
        torch.manual_seed(0)
        rb = self._get_rb(
            rb_type=rb_type, sampler=sampler, writer=writer, storage=storage, size=size
        )
        data = self._get_data(rb_type, size=5)
        rb.extend(data)
        new_data = rb.sample(3)
        if not isinstance(new_data, (torch.Tensor, TensorDictBase)):
            new_data = new_data[0]

        for d in new_data:
            found_similar = False
            for b in data:
                if isinstance(b, TensorDictBase):
                    keys = set(d.keys()).intersection(b.keys())
                    b = b.exclude("index").select(*keys, strict=False)
                    keys = set(d.keys()).intersection(b.keys())
                    d = d.select(*keys, strict=False)

                value = b == d
                if isinstance(value, (torch.Tensor, TensorDictBase)):
                    value = value.all()
                if value:
                    break
            else:
                raise RuntimeError("did not find match")

    def test_index(self, rb_type, sampler, writer, storage, size):
        torch.manual_seed(0)
        rb = self._get_rb(
            rb_type=rb_type, sampler=sampler, writer=writer, storage=storage, size=size
        )
        data = self._get_data(rb_type, size=5)
        rb.extend(data)
        d1 = rb[2]
        d2 = rb._storage[2]
        if type(d1) is not type(d2):
            d1 = d1[0]
        b = d1 == d2
        if not isinstance(b, bool):
            b = b.all()
        assert b


@pytest.mark.parametrize("max_size", [1000])
@pytest.mark.parametrize("shape", [[3, 4]])
@pytest.mark.parametrize("storage", [LazyTensorStorage, LazyMemmapStorage])
class TestStorages:
    def _get_nested_td(self, shape):
        nested_td = TensorDict(
            {
                "key1": torch.ones(*shape),
                "key2": torch.ones(*shape),
                "next": TensorDict(
                    {
                        "key1": torch.ones(*shape),
                        "key2": torch.ones(*shape),
                    },
                    shape,
                ),
            },
            shape,
        )
        return nested_td

    def test_init(self, max_size, shape, storage):
        td = self._get_nested_td(shape)
        mystorage = storage(max_size=max_size)
        mystorage._init(td)
        assert mystorage._storage.shape == (max_size, *shape)


@pytest.mark.parametrize("priority_key", ["pk", "td_error"])
@pytest.mark.parametrize("contiguous", [True, False])
@pytest.mark.parametrize("device", get_available_devices())
def test_prototype_prb(priority_key, contiguous, device):
    torch.manual_seed(0)
    np.random.seed(0)
    rb = rb_prototype.TensorDictReplayBuffer(
        sampler=samplers.PrioritizedSampler(5, alpha=0.7, beta=0.9),
        priority_key=priority_key,
    )
    td1 = TensorDict(
        source={
            "a": torch.randn(3, 1),
            priority_key: torch.rand(3, 1) / 10,
            "_idx": torch.arange(3).view(3, 1),
        },
        batch_size=[3],
    ).to(device)
    rb.extend(td1)
    s, _ = rb.sample(2)
    assert s.batch_size == torch.Size(
        [
            2,
        ]
    )
    assert (td1[s.get("_idx").squeeze()].get("a") == s.get("a")).all()
    assert_allclose_td(td1[s.get("_idx").squeeze()].select("a"), s.select("a"))

    # test replacement
    td2 = TensorDict(
        source={
            "a": torch.randn(5, 1),
            priority_key: torch.rand(5, 1) / 10,
            "_idx": torch.arange(5).view(5, 1),
        },
        batch_size=[5],
    ).to(device)
    rb.extend(td2)
    s, _ = rb.sample(5)
    assert s.batch_size == torch.Size([5])
    assert (td2[s.get("_idx").squeeze()].get("a") == s.get("a")).all()
    assert_allclose_td(td2[s.get("_idx").squeeze()].select("a"), s.select("a"))

    # test strong update
    # get all indices that match first item
    idx = s.get("_idx")
    idx_match = (idx == idx[0]).nonzero()[:, 0]
    s.set_at_(
        priority_key,
        torch.ones(
            idx_match.numel(),
            1,
            device=device,
        )
        * 100000000,
        idx_match,
    )
    val = s.get("a")[0]

    idx0 = s.get("_idx")[0]
    rb.update_tensordict_priority(s)
    s, _ = rb.sample(5)
    assert (val == s.get("a")).sum() >= 1
    torch.testing.assert_close(td2[idx0].get("a").view(1), s.get("a").unique().view(1))

    # test updating values of original td
    td2.set_("a", torch.ones_like(td2.get("a")))
    s, _ = rb.sample(5)
    torch.testing.assert_close(td2[idx0].get("a").view(1), s.get("a").unique().view(1))


@pytest.mark.parametrize("stack", [False, True])
def test_rb_prototype_trajectories(stack):
    traj_td = TensorDict(
        {"obs": torch.randn(3, 4, 5), "actions": torch.randn(3, 4, 2)},
        batch_size=[3, 4],
    )
    if stack:
        traj_td = torch.stack([td.to_tensordict() for td in traj_td], 0)

    rb = rb_prototype.TensorDictReplayBuffer(
        sampler=samplers.PrioritizedSampler(
            5,
            alpha=0.7,
            beta=0.9,
        ),
        priority_key="td_error",
    )
    rb.extend(traj_td)
    sampled_td, _ = rb.sample(3)
    sampled_td.set("td_error", torch.rand(3))
    rb.update_tensordict_priority(sampled_td)
    sampled_td, _ = rb.sample(3, include_info=True)
    assert (sampled_td.get("_weight") > 0).all()
    assert sampled_td.batch_size == torch.Size([3])

    # set back the trajectory length
    sampled_td_filtered = sampled_td.to_tensordict().exclude(
        "_weight", "index", "td_error"
    )
    sampled_td_filtered.batch_size = [3, 4]


@pytest.mark.parametrize(
    "rbtype,storage",
    [
        (ReplayBuffer, None),
        (ReplayBuffer, ListStorage),
        (PrioritizedReplayBuffer, None),
        (PrioritizedReplayBuffer, ListStorage),
        (TensorDictReplayBuffer, None),
        (TensorDictReplayBuffer, ListStorage),
        (TensorDictReplayBuffer, LazyTensorStorage),
        (TensorDictReplayBuffer, LazyMemmapStorage),
        (TensorDictPrioritizedReplayBuffer, None),
        (TensorDictPrioritizedReplayBuffer, ListStorage),
        (TensorDictPrioritizedReplayBuffer, LazyTensorStorage),
        (TensorDictPrioritizedReplayBuffer, LazyMemmapStorage),
    ],
)
@pytest.mark.parametrize("size", [3, 5, 100])
@pytest.mark.parametrize("prefetch", [0])
class TestBuffers:
    _default_params_rb = {}
    _default_params_td_rb = {}
    _default_params_prb = {"alpha": 0.8, "beta": 0.9}
    _default_params_td_prb = {"alpha": 0.8, "beta": 0.9}

    def _get_rb(self, rbtype, size, storage, prefetch):
        if storage is not None:
            storage = storage(size)
        if rbtype is ReplayBuffer:
            params = self._default_params_rb
        elif rbtype is PrioritizedReplayBuffer:
            params = self._default_params_prb
        elif rbtype is TensorDictReplayBuffer:
            params = self._default_params_td_rb
        elif rbtype is TensorDictPrioritizedReplayBuffer:
            params = self._default_params_td_prb
        else:
            raise NotImplementedError(rbtype)
        rb = rbtype(size=size, storage=storage, prefetch=prefetch, **params)
        return rb

    def _get_datum(self, rbtype):
        if rbtype is ReplayBuffer:
            data = torch.randint(100, (1,))
        elif rbtype is PrioritizedReplayBuffer:
            data = torch.randint(100, (1,))
        elif rbtype is TensorDictReplayBuffer:
            data = TensorDict({"a": torch.randint(100, (1,))}, [])
        elif rbtype is TensorDictPrioritizedReplayBuffer:
            data = TensorDict({"a": torch.randint(100, (1,))}, [])
        else:
            raise NotImplementedError(rbtype)
        return data

    def _get_data(self, rbtype, size):
        if rbtype is ReplayBuffer:
            data = [torch.randint(100, (1,)) for _ in range(size)]
        elif rbtype is PrioritizedReplayBuffer:
            data = [torch.randint(100, (1,)) for _ in range(size)]
        elif rbtype is TensorDictReplayBuffer:
            data = TensorDict(
                {
                    "a": torch.randint(100, (size,)),
                    "b": TensorDict({"c": torch.randint(100, (size,))}, [size]),
                },
                [size],
            )
        elif rbtype is TensorDictPrioritizedReplayBuffer:
            data = TensorDict(
                {
                    "a": torch.randint(100, (size,)),
                    "b": TensorDict({"c": torch.randint(100, (size,))}, [size]),
                },
                [size],
            )
        else:
            raise NotImplementedError(rbtype)
        return data

    def test_cursor_position2(self, rbtype, storage, size, prefetch):
        torch.manual_seed(0)
        rb = self._get_rb(rbtype, storage=storage, size=size, prefetch=prefetch)
        batch1 = self._get_data(rbtype, size=5)
        rb.extend(batch1)

        # Added less data than storage max size
        if size > 5:
            assert rb._cursor == 5
        # Added more data than storage max size
        elif size < 5:
            assert rb._cursor == 5 - size
        # Added as data as storage max size
        else:
            assert rb._cursor == 0
            batch2 = self._get_data(rbtype, size=size - 1)
            rb.extend(batch2)
            assert rb._cursor == size - 1

    def test_add(self, rbtype, storage, size, prefetch):
        torch.manual_seed(0)
        rb = self._get_rb(rbtype, storage=storage, size=size, prefetch=prefetch)
        data = self._get_datum(rbtype)
        rb.add(data)
        s = rb._storage[0]
        if isinstance(s, TensorDictBase):
            assert (s == data.select(*s.keys())).all()
        else:
            assert (s == data).all()

    def test_extend(self, rbtype, storage, size, prefetch):
        torch.manual_seed(0)
        rb = self._get_rb(rbtype, storage=storage, size=size, prefetch=prefetch)
        data = self._get_data(rbtype, size=5)
        rb.extend(data)
        length = len(rb)
        for d in data[-length:]:
            found_similar = False
            for b in rb._storage:
                if isinstance(b, TensorDictBase):
                    keys = set(d.keys()).intersection(b.keys())
                    b = b.exclude("index").select(*keys, strict=False)
                    keys = set(d.keys()).intersection(b.keys())
                    d = d.select(*keys, strict=False)

                value = b == d
                if isinstance(value, (torch.Tensor, TensorDictBase)):
                    value = value.all()
                if value:
                    break
            else:
                raise RuntimeError("did not find match")

    def test_sample(self, rbtype, storage, size, prefetch):
        torch.manual_seed(0)
        rb = self._get_rb(rbtype, storage=storage, size=size, prefetch=prefetch)
        data = self._get_data(rbtype, size=5)
        rb.extend(data)
        new_data = rb.sample(3)
        if not isinstance(new_data, (torch.Tensor, TensorDictBase)):
            new_data = new_data[0]

        for d in new_data:
            found_similar = False
            for b in data:
                if isinstance(b, TensorDictBase):
                    keys = set(d.keys()).intersection(b.keys())
                    b = b.exclude("index").select(*keys, strict=False)
                    keys = set(d.keys()).intersection(b.keys())
                    d = d.select(*keys, strict=False)

                value = b == d
                if isinstance(value, (torch.Tensor, TensorDictBase)):
                    value = value.all()
                if value:
                    break
            else:
                raise RuntimeError("did not find matching value")

    def test_index(self, rbtype, storage, size, prefetch):
        torch.manual_seed(0)
        rb = self._get_rb(rbtype, storage=storage, size=size, prefetch=prefetch)
        data = self._get_data(rbtype, size=5)
        rb.extend(data)
        d1 = rb[2]
        d2 = rb._storage[2]
        if type(d1) is not type(d2):
            d1 = d1[0]
        b = d1 == d2
        if not isinstance(b, bool):
            b = b.all()
        assert b


@pytest.mark.parametrize("priority_key", ["pk", "td_error"])
@pytest.mark.parametrize("contiguous", [True, False])
@pytest.mark.parametrize("device", get_available_devices())
def test_prb(priority_key, contiguous, device):
    torch.manual_seed(0)
    np.random.seed(0)
    rb = TensorDictPrioritizedReplayBuffer(
        5,
        alpha=0.7,
        beta=0.9,
        priority_key=priority_key,
    )
    td1 = TensorDict(
        source={
            "a": torch.randn(3, 1),
            priority_key: torch.rand(3, 1) / 10,
            "_idx": torch.arange(3).view(3, 1),
        },
        batch_size=[3],
    ).to(device)
    rb.extend(td1)
    s = rb.sample(2)
    assert s.batch_size == torch.Size(
        [
            2,
        ]
    )
    assert (td1[s.get("_idx").squeeze()].get("a") == s.get("a")).all()
    assert_allclose_td(td1[s.get("_idx").squeeze()].select("a"), s.select("a"))

    # test replacement
    td2 = TensorDict(
        source={
            "a": torch.randn(5, 1),
            priority_key: torch.rand(5, 1) / 10,
            "_idx": torch.arange(5).view(5, 1),
        },
        batch_size=[5],
    ).to(device)
    rb.extend(td2)
    s = rb.sample(5)
    assert s.batch_size == torch.Size([5])
    assert (td2[s.get("_idx").squeeze()].get("a") == s.get("a")).all()
    assert_allclose_td(td2[s.get("_idx").squeeze()].select("a"), s.select("a"))

    # test strong update
    # get all indices that match first item
    idx = s.get("_idx")
    idx_match = (idx == idx[0]).nonzero()[:, 0]
    s.set_at_(
        priority_key,
        torch.ones(
            idx_match.numel(),
            1,
            device=device,
        )
        * 100000000,
        idx_match,
    )
    val = s.get("a")[0]

    idx0 = s.get("_idx")[0]
    rb.update_priority(s)
    s = rb.sample(5)
    assert (val == s.get("a")).sum() >= 1
    torch.testing.assert_close(td2[idx0].get("a").view(1), s.get("a").unique().view(1))

    # test updating values of original td
    td2.set_("a", torch.ones_like(td2.get("a")))
    s = rb.sample(5)
    torch.testing.assert_close(td2[idx0].get("a").view(1), s.get("a").unique().view(1))


@pytest.mark.parametrize("stack", [False, True])
def test_rb_trajectories(stack):
    traj_td = TensorDict(
        {"obs": torch.randn(3, 4, 5), "actions": torch.randn(3, 4, 2)},
        batch_size=[3, 4],
    )
    if stack:
        traj_td = torch.stack([td.to_tensordict() for td in traj_td], 0)

    rb = TensorDictPrioritizedReplayBuffer(
        5,
        alpha=0.7,
        beta=0.9,
        priority_key="td_error",
    )
    rb.extend(traj_td)
    sampled_td = rb.sample(3)
    sampled_td.set("td_error", torch.rand(3))
    rb.update_priority(sampled_td)
    sampled_td = rb.sample(3, return_weight=True)
    assert (sampled_td.get("_weight") > 0).all()
    assert sampled_td.batch_size == torch.Size([3])

    # set back the trajectory length
    sampled_td_filtered = sampled_td.to_tensordict().exclude(
        "_weight", "index", "td_error"
    )
    sampled_td_filtered.batch_size = [3, 4]


def test_shared_storage_prioritized_sampler():

    n = 100

    storage = LazyMemmapStorage(n)
    writer = RoundRobinWriter()
    sampler0 = RandomSampler()
    sampler1 = PrioritizedSampler(max_capacity=n, alpha=0.7, beta=1.1)

    rb0 = rb_prototype.ReplayBuffer(
        storage=storage,
        writer=writer,
        sampler=sampler0,
    )
    rb1 = rb_prototype.ReplayBuffer(
        storage=storage,
        writer=writer,
        sampler=sampler1,
    )

    data = TensorDict({"a": torch.arange(50)}, [50])

    # Extend rb0. rb1 should be aware of changes to storage.
    rb0.extend(data)

    assert len(rb0) == 50
    assert len(storage) == 50
    assert len(rb1) == 50

    rb0.sample(10)
    rb1.sample(10)

    assert rb1._sampler._sum_tree.query(0, 10) == 10
    assert rb1._sampler._sum_tree.query(0, 50) == 50
    assert rb1._sampler._sum_tree.query(0, 70) == 50


def test_legacy_rb_does_not_attach():
    n = 10
    storage = LazyMemmapStorage(n)
    writer = RoundRobinWriter()
    sampler = RandomSampler()
    rb = ReplayBuffer(storage=storage, size=n, prefetch=0)
    prb = rb_prototype.ReplayBuffer(
        storage=storage,
        writer=writer,
        sampler=sampler,
    )

    assert len(storage._attached_entities) == 1
    assert prb in storage._attached_entities
    assert rb not in storage._attached_entities


def test_append_transform():
    rb = rb_prototype.ReplayBuffer(collate_fn=lambda x: torch.stack(x, 0))
    td = TensorDict(
        {
            "observation": torch.randn(2, 4, 3, 16),
            "observation2": torch.randn(2, 4, 3, 16),
        },
        [],
    )
    rb.add(td)
    flatten = CatTensors(
        in_keys=["observation", "observation2"], out_key="observation_cat"
    )

    rb.append_transform(flatten)

    sampled, _ = rb.sample(1)
    assert sampled.get("observation_cat").shape[-1] == 32


def test_init_transform():
    flatten = FlattenObservation(
        -2, -1, in_keys=["observation"], out_keys=["flattened"]
    )

    rb = rb_prototype.ReplayBuffer(
        collate_fn=lambda x: torch.stack(x, 0), transform=flatten
    )

    td = TensorDict({"observation": torch.randn(2, 4, 3, 16)}, [])
    rb.add(td)
    sampled, _ = rb.sample(1)
    assert sampled.get("flattened").shape[-1] == 48


def test_insert_transform():
    flatten = FlattenObservation(
        -2, -1, in_keys=["observation"], out_keys=["flattened"]
    )
    rb = rb_prototype.ReplayBuffer(
        collate_fn=lambda x: torch.stack(x, 0), transform=flatten
    )
    td = TensorDict({"observation": torch.randn(2, 4, 3, 16, 1)}, [])
    rb.add(td)

    rb.insert_transform(0, SqueezeTransform(-1, in_keys=["observation"]))

    sampled, _ = rb.sample(1)
    assert sampled.get("flattened").shape[-1] == 48

    with pytest.raises(ValueError):
        rb.insert_transform(10, SqueezeTransform(-1, in_keys=["observation"]))


transforms = [
    ToTensorImage,
    pytest.param(
        partial(RewardClipping, clamp_min=0.1, clamp_max=0.9), id="RewardClipping"
    ),
    BinarizeReward,
    pytest.param(
        partial(Resize, w=2, h=2),
        id="Resize",
        marks=pytest.mark.skipif(not _has_tv, reason="needs torchvision dependency"),
    ),
    pytest.param(
        partial(CenterCrop, w=1),
        id="CenterCrop",
        marks=pytest.mark.skipif(not _has_tv, reason="needs torchvision dependency"),
    ),
    pytest.param(
        partial(UnsqueezeTransform, unsqueeze_dim=-1), id="UnsqueezeTransform"
    ),
    pytest.param(partial(SqueezeTransform, squeeze_dim=-1), id="SqueezeTransform"),
    GrayScale,
    pytest.param(partial(ObservationNorm, loc=1, scale=2), id="ObservationNorm"),
    CatFrames,
    pytest.param(partial(RewardScaling, loc=1, scale=2), id="RewardScaling"),
    DoubleToFloat,
    VecNorm,
]


@pytest.mark.parametrize("transform", transforms)
def test_smoke_replay_buffer_transform(transform):
    rb = rb_prototype.ReplayBuffer(
        transform=transform(in_keys="observation"),
    )

    td = TensorDict({"observation": torch.randn(3, 3, 3, 16, 1)}, [])
    rb.add(td)
    rb.sample(1)

    rb._transform = mock.MagicMock()
    rb.sample(1)
    assert rb._transform.called


transforms = [
    partial(DiscreteActionProjection, max_n=1, m=1),
    FiniteTensorDictCheck,
    gSDENoise,
    PinMemoryTransform,
]


@pytest.mark.parametrize("transform", transforms)
def test_smoke_replay_buffer_transform_no_inkeys(transform):
    rb = rb_prototype.ReplayBuffer(
        collate_fn=lambda x: torch.stack(x, 0), transform=transform()
    )

    td = TensorDict({"observation": torch.randn(3, 3, 3, 16, 1)}, [])
    rb.add(td)
    rb.sample(1)

    rb._transform = mock.MagicMock()
    rb.sample(1)
    assert rb._transform.called


@pytest.mark.parametrize("size", [10, 15, 20])
@pytest.mark.parametrize("samples", [5, 9, 11, 14, 16])
@pytest.mark.parametrize("drop_last", [True, False])
def test_samplerwithoutrep(size, samples, drop_last):
    torch.manual_seed(0)
    storage = ListStorage(size)
    storage.set(range(size), range(size))
    assert len(storage) == size
    sampler = SamplerWithoutReplacement(drop_last=drop_last)
    visited = False
    for _ in range(10):
        _n_left = (
            sampler._sample_list.numel() if sampler._sample_list is not None else size
        )
        if samples > size and drop_last:
            with pytest.raises(
                ValueError,
                match=r"The batch size .* is greater than the storage capacity",
            ):
                idx, _ = sampler.sample(storage, samples)
            break
        idx, _ = sampler.sample(storage, samples)
        assert idx.numel() == samples
        if drop_last or _n_left >= samples:
            assert idx.unique().numel() == idx.numel()
        else:
            visited = True
    if not drop_last and (size % samples > 0):
        assert visited
    else:
        assert not visited


if __name__ == "__main__":
    args, unknown = argparse.ArgumentParser().parse_known_args()
    pytest.main([__file__, "--capture", "no", "--exitfirst"] + unknown)
