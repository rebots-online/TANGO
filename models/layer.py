import math
import torch
import torch.nn as nn
from torch.nn.utils import weight_norm
from typing import Any, Dict, List, Optional, Tuple, Type, Union


class Chomp1d(nn.Module):
    def __init__(self, chomp_size):
        super(Chomp1d, self).__init__()
        self.chomp_size = chomp_size

    def forward(self, x):
        return x[:, :, : -self.chomp_size].contiguous()


class TemporalBlock(nn.Module):
    def __init__(self, n_inputs, n_outputs, kernel_size, stride, dilation, padding, dropout=0.2):
        super(TemporalBlock, self).__init__()
        self.conv1 = weight_norm(nn.Conv1d(n_inputs, n_outputs, kernel_size, stride=stride, padding=padding, dilation=dilation))
        self.chomp1 = Chomp1d(padding)
        self.relu1 = nn.ReLU()
        self.dropout1 = nn.Dropout(dropout)

        self.conv2 = weight_norm(nn.Conv1d(n_outputs, n_outputs, kernel_size, stride=stride, padding=padding, dilation=dilation))
        self.chomp2 = Chomp1d(padding)
        self.relu2 = nn.ReLU()
        self.dropout2 = nn.Dropout(dropout)

        self.net = nn.Sequential(self.conv1, self.chomp1, self.relu1, self.dropout1, self.conv2, self.chomp2, self.relu2, self.dropout2)
        self.downsample = nn.Conv1d(n_inputs, n_outputs, 1) if n_inputs != n_outputs else None
        self.relu = nn.ReLU()
        self.init_weights()

    def init_weights(self):
        self.conv1.weight.data.normal_(0, 0.01)
        self.conv2.weight.data.normal_(0, 0.01)
        if self.downsample is not None:
            self.downsample.weight.data.normal_(0, 0.01)

    def forward(self, x):
        out = self.net(x)
        res = x if self.downsample is None else self.downsample(x)
        return self.relu(out + res)


class TemporalConvNet(nn.Module):
    def __init__(self, num_inputs, num_channels, kernel_size=2, dropout=0.2):
        super(TemporalConvNet, self).__init__()
        layers = []
        num_levels = len(num_channels)
        for i in range(num_levels):
            dilation_size = 2**i
            in_channels = num_inputs if i == 0 else num_channels[i - 1]
            out_channels = num_channels[i]
            layers += [
                TemporalBlock(
                    in_channels,
                    out_channels,
                    kernel_size,
                    stride=1,
                    dilation=dilation_size,
                    padding=(kernel_size - 1) * dilation_size,
                    dropout=dropout,
                )
            ]

        self.network = nn.Sequential(*layers)

    def forward(self, x):
        return self.network(x)


class TextEncoderTCN(nn.Module):
    """
    based on https://github.com/locuslab/TCN/blob/master/TCN/word_cnn/model.py
    Licensed under: https://github.com/locuslab/TCN/blob/master/LICENSE
    """

    def __init__(
        self, args, n_words=11195, embed_size=300, pre_trained_embedding=None, kernel_size=2, dropout=0.3, emb_dropout=0.1, word_cache=False
    ):
        super(TextEncoderTCN, self).__init__()
        num_channels = [args.hidden_size]  # * args.n_layer
        self.tcn = TemporalConvNet(embed_size, num_channels, kernel_size, dropout=dropout)
        self.decoder = nn.Linear(num_channels[-1], args.word_f)
        self.drop = nn.Dropout(emb_dropout)
        self.init_weights()

    def init_weights(self):
        self.decoder.bias.data.fill_(0)
        self.decoder.weight.data.normal_(0, 0.01)

    def forward(self, input):
        y = self.tcn(input.transpose(1, 2)).transpose(1, 2)
        y = self.decoder(y)
        return y, torch.max(y, dim=1)[0]


def reparameterize(mu, logvar):
    std = torch.exp(0.5 * logvar)
    eps = torch.randn_like(std)
    return mu + eps * std


def ConvNormRelu(in_channels, out_channels, downsample=False, padding=0, batchnorm=True):
    if not downsample:
        k = 3
        s = 1
    else:
        k = 4
        s = 2
    conv_block = nn.Conv1d(in_channels, out_channels, kernel_size=k, stride=s, padding=padding)
    norm_block = nn.BatchNorm1d(out_channels)
    if batchnorm:
        net = nn.Sequential(conv_block, norm_block, nn.LeakyReLU(0.2, True))
    else:
        net = nn.Sequential(conv_block, nn.LeakyReLU(0.2, True))
    return net


class BasicBlock(nn.Module):
    """
    based on timm: https://github.com/huggingface/pytorch-image-models/blob/f689c850b90b16a45cc119a7bc3b24375636fc63/timm/models/resnet.py#L34
    Licensed under: https://github.com/huggingface/pytorch-image-models/blob/main/LICENSE
    """

    def __init__(
        self,
        inplanes: int,
        planes: int,
        ker_size: int,  # add kernel size
        stride: int = 1,
        downsample: Optional[nn.Module] = None,
        cardinality: int = 1,
        base_width: int = 64,
        reduce_first: int = 1,
        dilation: int = 1,
        first_dilation: Optional[int] = None,
        act_layer: Type[nn.Module] = nn.LeakyReLU,  # change activation function from ReLU to LeakyReLU
        norm_layer: Type[nn.Module] = nn.BatchNorm1d,  # change norm layer from BatchNorm2d to BatchNorm1d
        attn_layer: Optional[Type[nn.Module]] = None,
        aa_layer: Optional[Type[nn.Module]] = None,
        drop_block: Optional[Type[nn.Module]] = None,
        drop_path: Optional[nn.Module] = None,
    ):
        super(BasicBlock, self).__init__()

        """
        Original Layer definition:
        https://github.com/huggingface/pytorch-image-models/blob/f689c850b90b16a45cc119a7bc3b24375636fc63/timm/models/resnet.py#L75C1-L100C35
        """
        # Revised from here
        self.conv1 = nn.Conv1d(inplanes, planes, kernel_size=ker_size, stride=stride, padding=first_dilation, dilation=dilation, bias=True)
        self.bn1 = norm_layer(planes)
        self.act1 = act_layer(inplace=True)
        self.conv2 = nn.Conv1d(planes, planes, kernel_size=ker_size, padding=ker_size // 2, dilation=dilation, bias=True)
        self.bn2 = norm_layer(planes)
        self.act2 = act_layer(inplace=True)
        if downsample is not None:
            self.downsample = nn.Sequential(
                nn.Conv1d(inplanes, planes, stride=stride, kernel_size=ker_size, padding=first_dilation, dilation=dilation, bias=True),
                norm_layer(planes),
            )
        else:
            self.downsample = None
        self.stride = stride
        self.dilation = dilation
        self.drop_block = drop_block
        self.drop_path = drop_path
        # Until here

    def zero_init_last(self):
        if getattr(self.bn2, "weight", None) is not None:
            nn.init.zeros_(self.bn2.weight)

    def forward(self, x: torch.Tensor) -> torch.Tensor:
        shortcut = x
        """
        Original Layer Sequence:
        https://github.com/huggingface/pytorch-image-models/blob/f689c850b90b16a45cc119a7bc3b24375636fc63/timm/models/resnet.py#L209C1-L226C34
        """
        # Revised from here
        x = self.conv1(x)
        x = self.bn1(x)
        x = self.act1(x)
        x = self.conv2(x)
        x = self.bn2(x)
        # Until here

        if self.downsample is not None:
            shortcut = self.downsample(shortcut)
        x += shortcut
        x = self.act2(x)
        return x


def init_weight(m):
    if isinstance(m, nn.Conv1d) or isinstance(m, nn.Linear) or isinstance(m, nn.ConvTranspose1d):
        nn.init.xavier_normal_(m.weight)
        # m.bias.data.fill_(0.01)
        if m.bias is not None:
            nn.init.constant_(m.bias, 0)


def init_weight_skcnn(m):
    if isinstance(m, nn.Conv1d) or isinstance(m, nn.Linear) or isinstance(m, nn.ConvTranspose1d):
        nn.init.kaiming_uniform_(m.weight, a=math.sqrt(5))
        # m.bias.data.fill_(0.01)
        if m.bias is not None:
            # nn.init.constant_(m.bias, 0)
            fan_in, _ = nn.init._calculate_fan_in_and_fan_out(m.weight)
            bound = 1 / math.sqrt(fan_in)
            nn.init.uniform_(m.bias, -bound, bound)


class ResBlock(nn.Module):
    def __init__(self, channel):
        super(ResBlock, self).__init__()
        self.model = nn.Sequential(
            nn.Conv1d(channel, channel, kernel_size=3, stride=1, padding=1),
            nn.LeakyReLU(0.2, inplace=True),
            nn.Conv1d(channel, channel, kernel_size=3, stride=1, padding=1),
        )

    def forward(self, x):
        residual = x
        out = self.model(x)
        out += residual
        return out
