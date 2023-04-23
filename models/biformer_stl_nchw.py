

from collections import OrderedDict
from functools import partial
from typing import Tuple, Union

import torch
import torch.nn as nn
from einops.layers.torch import Rearrange
from fairscale.nn.checkpoint import checkpoint_wrapper
from timm.models import register_model
from timm.models.layers import DropPath, LayerNorm2d, to_2tuple, trunc_normal_

from ops.bra_nchw import nchwBRA
from ._common import nchwAttentionLePE


class BiFormerBlock(nn.Module):
    """
    Attention + FFN
    """
    def __init__(self, dim, drop_path=0., num_heads=8, n_win=7, 
                       qk_scale=None, topk=4, mlp_ratio=4, side_dwconv=5, 
                       norm_layer=LayerNorm2d):

        super().__init__()
        self.norm1 = norm_layer(dim) # important to avoid attention collapsing
        
        if topk > 0:
            self.attn = nchwBRA(dim=dim, num_heads=num_heads, n_win=n_win,
                qk_scale=qk_scale, topk=topk, side_dwconv=side_dwconv)
        elif topk == -1:
            self.attn = nchwAttentionLePE(dim=dim)
        else:
            raise ValueError('topk should >0 or =-1 !')

        #local feature block
        # self.local = Local_block(dim=dim)
        # self.conv1 = nn.Conv2d(dim*2, dim, kernel_size=1, stride=1, padding=1, bias=False)
        # local feature block
        self.norm2 = norm_layer(dim)
        self.mlp = nn.Sequential(nn.Conv2d(dim, int(mlp_ratio*dim), kernel_size=1),
                                 nn.GELU(),
                                 nn.Conv2d(int(mlp_ratio*dim), dim, kernel_size=1)
                                )
        self.drop_path = DropPath(drop_path) if drop_path > 0. else nn.Identity()
            

    def forward(self, x):
        """
        Args:
            x: NCHW tensor
        Return:
            NCHW tensor
        """
        # attention & mlp
        x = x + self.drop_path(self.attn(self.norm1(x))) # (N, C, H, W)
        # local feature block
        # glob = self.attn(self.norm1(x)) # (N, C, H, W)
        # local = self.local(self.norm1(x))
        # x = x+ self.drop_path(self.conv1(torch.cat([glob, local], dim=1)))
        # # local feature block
        x = x + self.drop_path(self.mlp(self.norm2(x))) # (N, C, H, W)
        return x

class BasicLayer(nn.Module):
    """
    Stack several BiFormer Blocks
    """
    def __init__(self, dim, depth, num_heads, n_win, topk,
                 mlp_ratio=4., drop_path=0., side_dwconv=5):

        super().__init__()
        self.dim = dim
        self.depth = depth

        self.blocks = nn.ModuleList([
            BiFormerBlock(
                    dim=dim,
                    drop_path=drop_path[i] if isinstance(drop_path, list) else drop_path,
                    num_heads=num_heads,
                    n_win=n_win,
                    topk=topk,
                    mlp_ratio=mlp_ratio,
                    side_dwconv=side_dwconv,
                )
            for i in range(depth)
        ])

    def forward(self, x:torch.Tensor):
        """
        Args:
            x: NCHW tensor
        Return:
            NCHW tensor
        """
        for blk in self.blocks:
            x = blk(x)
        return x

    def extra_repr(self) -> str:
        return f"dim={self.dim}, depth={self.depth}"

class LayerNorm(nn.Module):
    r""" LayerNorm that supports two data formats: channels_last (default) or channels_first.
    The ordering of the dimensions in the inputs. channels_last corresponds to inputs with
    shape (batch_size, height, width, channels) while channels_first corresponds to inputs
    with shape (batch_size, channels, height, width).
    """

    def __init__(self, normalized_shape, eps=1e-6, data_format="channels_last"):
        super().__init__()
        self.weight = nn.Parameter(torch.ones(normalized_shape), requires_grad=True)
        self.bias = nn.Parameter(torch.zeros(normalized_shape), requires_grad=True)
        self.eps = eps
        self.data_format = data_format
        if self.data_format not in ["channels_last", "channels_first"]:
            raise ValueError(f"not support data format '{self.data_format}'")
        self.normalized_shape = (normalized_shape,)

    def forward(self, x: torch.Tensor) -> torch.Tensor:
        if self.data_format == "channels_last":
            return torch.nn.functional.layer_norm(x, self.normalized_shape, self.weight, self.bias, self.eps)
        elif self.data_format == "channels_first":
            # [batch_size, channels, height, width]
            mean = x.mean(1, keepdim=True)
            var = (x - mean).pow(2).mean(1, keepdim=True)
            x = (x - mean) / torch.sqrt(var + self.eps)
            x = self.weight[:, None, None] * x + self.bias[:, None, None]
            return x

class Local_block(nn.Module):
    r""" Local Feature Block. There are two equivalent implementations:
    (1) DwConv -> LayerNorm (channels_first) -> 1x1 Conv -> GELU -> 1x1 Conv; all in (N, C, H, W)
    (2) DwConv -> Permute to (N, H, W, C); LayerNorm (channels_last) -> Linear -> GELU -> Linear; Permute back
    We use (2) as we find it slightly faster in PyTorch

    Args:
        dim (int): Number of input channels.
        drop_rate (float): Stochastic depth rate. Default: 0.0
    """
    def __init__(self, dim, drop_rate=0.):
        super().__init__()
        self.dwconv = nn.Conv2d(dim, dim, kernel_size=3, padding=1, groups=dim)  # depthwise conv
        self.norm = LayerNorm(dim, eps=1e-6, data_format="channels_last")
        self.pwconv = nn.Linear(dim, dim)  # pointwise/1x1 convs, implemented with linear layers
        self.act = nn.GELU()
        self.drop_path = DropPath(drop_rate) if drop_rate > 0. else nn.Identity()

    def forward(self, x: torch.Tensor) -> torch.Tensor:
        shortcut = x
        x = self.dwconv(x)
        x = x.permute(0, 2, 3, 1)  # [N, C, H, W] -> [N, H, W, C]
        x = self.norm(x)
        x = self.pwconv(x)
        x = self.act(x)
        x = x.permute(0, 3, 1, 2)  # [N, H, W, C] -> [N, C, H, W]
        x = shortcut + self.drop_path(x)
        return x

class nchwBiFormerSTL(nn.Module):
    """
    Replace WindowAttn-ShiftWindowAttn in Swin-T model with Bi-Level Routing Attention
    """
    def __init__(self, in_chans=3, num_classes=1000,
                 depth=[2, 2, 6, 2],
                 embed_dim=[96, 192, 384, 768],
                 head_dim=32, qk_scale=None,
                 drop_path_rate=0., drop_rate=0.,
                 use_checkpoint_stages=[],
                 # before_attn_dwconv=3,
                 mlp_ratios=[4, 4, 4, 4],
                 norm_layer=LayerNorm2d,
                 pre_head_norm_layer=None,
                 ######## biformer specific ############
                 n_wins:Union[int, Tuple[int]]=(7, 7, 7, 7),
                 topks:Union[int, Tuple[int]]=(1, 4, 16, -2),
                 side_dwconv:int=5,
                 #######################################
                 ):
        super().__init__()
        self.num_classes = num_classes
        self.num_features = self.embed_dim = embed_dim  # num_features for consistency with other models

        ############ downsample layers (patch embeddings) ######################
        self.downsample_layers = nn.ModuleList()
        # patch embedding: conv-norm
        stem = nn.Sequential(nn.Conv2d(in_chans, embed_dim[0], kernel_size=(4, 4), stride=(4, 4)),
                             norm_layer(embed_dim[0])
                            )
        if use_checkpoint_stages:
            stem = checkpoint_wrapper(stem)
        self.downsample_layers.append(stem)

        for i in range(3):
            # patch merging: norm-conv
            downsample_layer = nn.Sequential(
                        norm_layer(embed_dim[i]), 
                        nn.Conv2d(embed_dim[i], embed_dim[i+1], kernel_size=(2, 2), stride=(2, 2)),
                    )
            if use_checkpoint_stages:
                downsample_layer = checkpoint_wrapper(downsample_layer)
            self.downsample_layers.append(downsample_layer)

        ##########################################################################
        self.stages = nn.ModuleList() # 4 feature resolution stages, each consisting of multiple residual blocks
        nheads= [dim // head_dim for dim in embed_dim]
        dp_rates=[x.item() for x in torch.linspace(0, drop_path_rate, sum(depth))]

        for i in range(4):
            stage = BasicLayer(dim=embed_dim[i],
                               depth=depth[i],
                               num_heads=nheads[i], 
                               mlp_ratio=mlp_ratios[i],
                               drop_path=dp_rates[sum(depth[:i]):sum(depth[:i+1])],
                               ####### biformer specific ########
                               n_win=n_wins[i], topk=topks[i], side_dwconv=side_dwconv
                               ##################################
                               )
            if i in use_checkpoint_stages:
                stage = checkpoint_wrapper(stage)
            self.stages.append(stage)

        ##########################################################################
        pre_head_norm = pre_head_norm_layer or norm_layer 
        self.norm = pre_head_norm(embed_dim[-1])
        # Classifier head
        self.head = nn.Linear(embed_dim[-1], num_classes) if num_classes > 0 else nn.Identity()
        self.apply(self._init_weights)

    def _init_weights(self, m):
        if isinstance(m, nn.Linear):
            trunc_normal_(m.weight, std=.02)
            if isinstance(m, nn.Linear) and m.bias is not None:
                nn.init.constant_(m.bias, 0)
        elif isinstance(m, nn.LayerNorm):
            nn.init.constant_(m.bias, 0)
            nn.init.constant_(m.weight, 1.0)

    @torch.jit.ignore
    def no_weight_decay(self):
        return {'pos_embed', 'cls_token'}

    def get_classifier(self):
        return self.head

    def reset_classifier(self, num_classes, global_pool=''):
        self.num_classes = num_classes
        self.head = nn.Linear(self.embed_dim, num_classes) if num_classes > 0 else nn.Identity()

    def forward_features(self, x:torch.Tensor):
        for i in range(4):
            x = self.downsample_layers[i](x)
            x = self.stages[i](x)
        x = self.norm(x)
        return x

    def forward(self, x:torch.Tensor):
        x = self.forward_features(x)
        # x = x.flatten(2).mean(-1)
        x = x.mean([2, 3])
        x = self.head(x)
        return x


model_urls = {
    "biformer_stl_nchw_in1k": 'https://matix.li/216749d857fd',
}

@register_model
def biformer_stl_nchw(pretrained=False, pretrained_cfg=None,
                 pretrained_cfg_overlay=None, **kwargs):
    model = nchwBiFormerSTL(depth=[2, 2, 6, 2],
                        embed_dim=[96, 192, 384, 768],
                        mlp_ratios=[4, 4, 4, 4],
                        head_dim=32,
                        norm_layer=nn.BatchNorm2d,
                        ######## biformer specific ############
                        n_wins=(7, 7, 7, 7),
                        topks=(1, 4, 16, -1),
                        side_dwconv=5,
                        #######################################
                        **kwargs)
    if pretrained:
        model_key = 'biformer_stl_nchw_in1k'
        url = model_urls[model_key]
        checkpoint = torch.hub.load_state_dict_from_url(url=url, map_location="cpu", check_hash=True, file_name=f"{model_key}.pth")
        model.load_state_dict(checkpoint["model"])

    return model
