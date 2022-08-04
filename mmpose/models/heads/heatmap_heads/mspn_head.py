# Copyright (c) OpenMMLab. All rights reserved.
import copy
from typing import List, Optional, Sequence

import torch
from mmcv.cnn import (ConvModule, DepthwiseSeparableConvModule, Linear,
                      build_activation_layer, build_norm_layer)
from mmengine.data import PixelData
from torch import Tensor, nn

from mmpose.evaluation.functional import pose_pck_accuracy
from mmpose.registry import KEYPOINT_CODECS, MODELS
from mmpose.utils.tensor_utils import to_numpy
from mmpose.utils.typing import (ConfigType, MultiConfig, OptConfigType,
                                 OptSampleList, SampleList)
from ..base_head import BaseHead

OptIntSeq = Optional[Sequence[int]]


class PRM(nn.Module):
    """Pose Refine Machine.

    Please refer to "Learning Delicate Local Representations
    for Multi-Person Pose Estimation" (ECCV 2020).

    Args:
        out_channels (int): Number of the output channels, equals to
            the number of keypoints.
        norm_cfg (Config): Config to construct the norm layer.
            Defaults to ``dict(type='BN')``
    """

    def __init__(self,
                 out_channels: int,
                 norm_cfg: ConfigType = dict(type='BN')):
        super().__init__()

        # Protect mutable default arguments
        norm_cfg = copy.deepcopy(norm_cfg)
        self.out_channels = out_channels
        self.global_pooling = nn.AdaptiveAvgPool2d((1, 1))
        self.middle_path = nn.Sequential(
            Linear(self.out_channels, self.out_channels),
            build_norm_layer(dict(type='BN1d'), out_channels)[1],
            build_activation_layer(dict(type='ReLU')),
            Linear(self.out_channels, self.out_channels),
            build_norm_layer(dict(type='BN1d'), out_channels)[1],
            build_activation_layer(dict(type='ReLU')),
            build_activation_layer(dict(type='Sigmoid')))

        self.bottom_path = nn.Sequential(
            ConvModule(
                self.out_channels,
                self.out_channels,
                kernel_size=1,
                stride=1,
                padding=0,
                norm_cfg=norm_cfg,
                inplace=False),
            DepthwiseSeparableConvModule(
                self.out_channels,
                1,
                kernel_size=9,
                stride=1,
                padding=4,
                norm_cfg=norm_cfg,
                inplace=False), build_activation_layer(dict(type='Sigmoid')))
        self.conv_bn_relu_prm_1 = ConvModule(
            self.out_channels,
            self.out_channels,
            kernel_size=3,
            stride=1,
            padding=1,
            norm_cfg=norm_cfg,
            inplace=False)

    def forward(self, x: Tensor) -> Tensor:
        """Forward the network. The input heatmaps will be refined.

        Args:
            x (Tensor): The input heatmaps.

        Returns:
            Tensor: output heatmaps.
        """
        out = self.conv_bn_relu_prm_1(x)
        out_1 = out

        out_2 = self.global_pooling(out_1)
        out_2 = out_2.view(out_2.size(0), -1)
        out_2 = self.middle_path(out_2)
        out_2 = out_2.unsqueeze(2)
        out_2 = out_2.unsqueeze(3)

        out_3 = self.bottom_path(out_1)
        out = out_1 * (1 + out_2 * out_3)

        return out


class PredictHeatmap(nn.Module):
    """Predict the heatmap for an input feature.

    Args:
        unit_channels (int): Number of input channels.
        out_channels (int): Number of output channels.
        out_shape (tuple): Shape of the output heatmaps.
        use_prm (bool): Whether to use pose refine machine. Default: False.
        norm_cfg (Config): Config to construct the norm layer.
            Defaults to ``dict(type='BN')``
    """

    def __init__(self,
                 unit_channels: int,
                 out_channels: int,
                 out_shape: tuple,
                 use_prm: bool = False,
                 norm_cfg: ConfigType = dict(type='BN')):

        super().__init__()

        # Protect mutable default arguments
        norm_cfg = copy.deepcopy(norm_cfg)
        self.unit_channels = unit_channels
        self.out_channels = out_channels
        self.out_shape = out_shape
        self.use_prm = use_prm
        if use_prm:
            self.prm = PRM(out_channels, norm_cfg=norm_cfg)
        self.conv_layers = nn.Sequential(
            ConvModule(
                unit_channels,
                unit_channels,
                kernel_size=1,
                stride=1,
                padding=0,
                norm_cfg=norm_cfg,
                inplace=False),
            ConvModule(
                unit_channels,
                out_channels,
                kernel_size=3,
                stride=1,
                padding=1,
                norm_cfg=norm_cfg,
                act_cfg=None,
                inplace=False))

    def forward(self, feature: Tensor) -> Tensor:
        """Forward the network.

        Args:
            feature (Tensor): The input feature maps.

        Returns:
            Tensor: output heatmaps.
        """
        feature = self.conv_layers(feature)
        output = nn.functional.interpolate(
            feature, size=self.out_shape, mode='bilinear', align_corners=True)
        if self.use_prm:
            output = self.prm(output)
        return output


@MODELS.register_module()
class MSPNHead(BaseHead):
    """Multi-stage multi-unit heatmap head introduced in `Multi-Stage Pose
    estimation Network (MSPN)`_ by Li et al (2019), and used by `Residual Steps
    Networks (RSN)`_ by Cai et al (2020). The head consists of multiple stages
    and each stage consists of multiple units. Each unit of each stage has some
    conv layers.

    Args:
        num_stages (int): Number of stages.
        num_units (int): Number of units in each stage.
        out_shape (tuple): The output shape of the output heatmaps.
        unit_channels (int): Number of input channels.
        out_channels (int): Number of output channels.
        out_shape (tuple): Shape of the output heatmaps.
        use_prm (bool): Whether to use pose refine machine (PRM).
            Defaults to ``False``.
        norm_cfg (Config): Config to construct the norm layer.
            Defaults to ``dict(type='BN')``
        loss (Config | List[Config]): Config of the keypoint loss for
            different stages and different units.
            Defaults to use :class:`KeypointMSELoss`.
        decoder (Config, optional): The decoder config that controls decoding
            keypoint coordinates from the network output. Defaults to ``None``
        init_cfg (Config, optional): Config to control the initialization. See
            :attr:`default_init_cfg` for default settings

    .. _`MSPN`: https://arxiv.org/abs/1901.00148
    .. _`RSN`: https://arxiv.org/abs/2003.04030
    """
    _version = 2

    def __init__(self,
                 num_stages: int = 4,
                 num_units: int = 4,
                 out_shape: tuple = (64, 48),
                 unit_channels: int = 256,
                 out_channels: int = 17,
                 use_prm: bool = False,
                 norm_cfg: ConfigType = dict(type='BN'),
                 loss: MultiConfig = dict(
                     type='KeypointMSELoss', use_target_weight=True),
                 decoder: OptConfigType = None,
                 init_cfg: OptConfigType = None):
        if init_cfg is None:
            init_cfg = self.default_init_cfg
        super().__init__(init_cfg)

        self.num_stages = num_stages
        self.num_units = num_units
        self.out_shape = out_shape
        self.unit_channels = unit_channels
        self.out_channels = out_channels

        if isinstance(loss, list) and len(loss) != num_stages * num_units:
            raise ValueError(
                f'The length of loss_module({len(loss)}) did not match '
                f'`num_stages`({num_stages}) * `num_units` ({num_units})')

        self.loss_module = MODELS.build(loss)
        if decoder is not None:
            self.decoder = KEYPOINT_CODECS.build(decoder)
        else:
            self.decoder = None

        # Protect mutable default arguments
        norm_cfg = copy.deepcopy(norm_cfg)

        self.predict_layers = nn.ModuleList([])
        for i in range(self.num_stages):
            for j in range(self.num_units):
                self.predict_layers.append(
                    PredictHeatmap(
                        unit_channels,
                        out_channels,
                        out_shape,
                        use_prm,
                        norm_cfg=norm_cfg))

    @property
    def default_init_cfg(self):
        """Default config for weight initialization."""
        init_cfg = [
            dict(type='Kaiming', layer='Conv2d'),
            dict(type='Normal', layer='Linear', std=0.01),
            dict(type='Constant', layer='BatchNorm2d', val=1),
        ]
        return init_cfg

    def forward(self, feats: Sequence[Sequence[Tensor]]) -> List[Tensor]:
        """Forward the network. The input is multi-stage multi-unit feature
        maps and the output is a list of heatmaps from multiple stages.

        Args:
            feats (Sequence[Sequence[Tensor]]): Feature maps from multiple
                stages and units.

        Returns:
            List[Tensor]: A list of output heatmaps from multiple stages
                and units.
        """
        out = []
        assert len(feats) == self.num_stages, (
            f'The length of feature maps did not match the '
            f'`num_stages` in {self.__class__.__name__}')
        for feat in feats:
            assert len(feat) == self.num_units, (
                f'The length of feature maps did not match the '
                f'`num_units` in {self.__class__.__name__}')
            for f in feat:
                assert f.shape[1] == self.unit_channels, (
                    f'The number of feature map channels did not match the '
                    f'`unit_channels` in {self.__class__.__name__}')

        for i in range(self.num_stages):
            for j in range(self.num_units):
                y = self.predict_layers[i * self.num_units + j](feats[i][j])
                out.append(y)
        return out

    def predict(self,
                feats: Sequence[Sequence[Tensor]],
                batch_data_samples: OptSampleList,
                test_cfg: OptConfigType = {}) -> SampleList:
        """Predict results from multi-stage feature maps.

        Args:
            feats (Sequence[Sequence[Tensor]]): Feature maps from multiple
                stages and units.
            batch_data_samples (List[:obj:`PoseDataSample`]): The Data
                Samples. It usually includes information such as
                `gt_instances`.
            test_cfg (Config, optional): The testing/inference config.

        Returns:
            List[:obj:`PoseDataSample`]: Pose estimation results of each
            sample after the post process.
        """
        # multi-stage multi-unit batch heatmaps
        msmu_batch_heatmaps = self.forward(feats)
        batch_heatmaps = msmu_batch_heatmaps[-1]

        preds = self.decode(batch_heatmaps, batch_data_samples)

        # Whether to visualize the predicted heatmaps
        if test_cfg.get('output_heatmaps', False):
            for heatmaps, data_sample in zip(batch_heatmaps, preds):
                # Store the heatmap predictions in the data sample
                if 'pred_fileds' not in data_sample:
                    data_sample.pred_fields = PixelData()
                data_sample.pred_fields.heatmaps = heatmaps

        return preds

    def loss(self,
             feats: Sequence[Sequence[Tensor]],
             batch_data_samples: OptSampleList,
             train_cfg: OptConfigType = {}) -> dict:
        """Calculate losses from a batch of inputs and data samples.

        Note:
            - batch_size: B
            - num_output_heatmap_levels: L
            - num_keypoints: K
            - heatmaps height: H
            - heatmaps weight: W
            - num_instances: N (usually 1 in topdown heatmap heads)

        Args:
            feats (Sequence[Sequence[Tensor]]): Feature maps from multiple
                stages and units.
            batch_data_samples (List[:obj:`PoseDataSample`]): The Data
                Samples. It usually includes information such as
                `gt_instances`.
            train_cfg (Config, optional): The training config.

        Returns:
            dict: A dictionary of loss components.
        """
        # multi-stage multi-unit predict heatmaps
        msmu_pred_heatmaps = self.forward(feats)

        gt_heatmaps = torch.stack([
            d.gt_fields.heatmaps for d in batch_data_samples
        ])  # shape: [B, L*K, H, W]
        keypoint_weights = torch.cat([
            d.gt_instances.keypoint_weights for d in batch_data_samples
        ])  # shape: [B*N, L, K]

        # number of output channels for each level of gt heatmaps,
        # usually equals to the number of keypoints
        K = self.out_channels

        # calculate losses over multiple stages and multiple units
        losses = dict()
        for i in range(self.num_stages * self.num_units):
            if isinstance(self.loss_module, nn.Sequential):
                # use different loss_module over different stages and units
                loss_func = self.loss_module[i]
            else:
                # use the same loss_module over different stages and units
                loss_func = self.loss_module

            # the `gt_heatmaps` used to calculate loss for different stages
            # and different units are different, but the `keypoint_weights`
            # are the same
            loss_i = loss_func(msmu_pred_heatmaps[i],
                               gt_heatmaps[:, i * K:(i + 1) * K],
                               keypoint_weights[:, i])

            if 'loss_kpt' not in losses:
                losses['loss_kpt'] = loss_i
            else:
                losses['loss_kpt'] += loss_i

        # calculate accuracy
        _, avg_acc, _ = pose_pck_accuracy(
            output=to_numpy(msmu_pred_heatmaps[-1]),
            target=to_numpy(gt_heatmaps[:, -K:]),
            mask=to_numpy(keypoint_weights[:, -1]) > 0)

        losses.update(acc_pose=float(avg_acc))

        return losses