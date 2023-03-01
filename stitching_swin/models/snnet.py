import torch
import torch.nn as nn
from collections import defaultdict
import numpy as np


def unpaired_stitching(front_depth=12, end_depth=24):
    num_stitches = front_depth

    block_ids = torch.tensor(list(range(front_depth)))
    block_ids = block_ids[None, None, :].float()
    end_mapping_ids = torch.nn.functional.interpolate(block_ids, end_depth)
    end_mapping_ids = end_mapping_ids.squeeze().long().tolist()
    front_mapping_ids = block_ids.squeeze().long().tolist()

    stitch_cfgs = []
    for idx in front_mapping_ids:
        for i, e_idx in enumerate(end_mapping_ids):
            if idx != e_idx or idx >= i:
                continue
            else:
                stitch_cfgs.append((idx, i))
    return stitch_cfgs, end_mapping_ids, num_stitches


def paired_stitching(depth=12, kernel_size=2, stride=1):
    blk_id = list(range(depth))
    i = 0
    stitch_cfgs = []
    stitch_id = -1
    stitching_layers_mappings = []

    while i < depth:
        ids = blk_id[i:i + kernel_size]
        has_new_stitches = False
        for j in ids:
            for k in ids:
                if (j, k) not in stitch_cfgs:
                    if j >= k:
                        continue
                    has_new_stitches = True
                    stitch_cfgs.append((j, k))
                    stitching_layers_mappings.append(stitch_id + 1)

        if has_new_stitches:
            stitch_id += 1

        i += stride

    num_stitches = stitch_id + 1
    return stitch_cfgs, stitching_layers_mappings, num_stitches

def get_stitch_configs(depths, stage_id):
    depths = sorted(depths)

    d = depths[0]
    total_configs = []
    total_stitches = []

    for i in range(1, len(depths)):
        next_d = depths[i]
        if next_d == d:
            stitch_cfgs, layers_mappings, num_stitches = paired_stitching(d)
        else:
            stitch_cfgs, layers_mappings, num_stitches = unpaired_stitching(d, next_d)
        comb = (i-1, i)
        for cfg, layer_mapping_id in zip(stitch_cfgs, layers_mappings):
            total_configs.append({
                'comb_id': comb,
                'stage_id': stage_id,
                'stitch_cfgs': [cfg],
                'stitch_layers': [layer_mapping_id]
            })
        total_stitches.append((num_stitches, comb))
        d = next_d

    return total_configs, total_stitches

def rearrange_activations(activations):
    n_channels = activations.shape[-1]
    activations = activations.reshape(-1, n_channels)
    return activations


def ps_inv(x1, x2):
    '''Least-squares solver given feature maps from two anchors.
    
    Source: https://github.com/renyi-ai/drfrankenstein/blob/main/src/comparators/compare_functions/ps_inv.py
    '''
    x1 = rearrange_activations(x1)
    x2 = rearrange_activations(x2)

    if not x1.shape[0] == x2.shape[0]:
        raise ValueError('Spatial size of compared neurons must match when ' \
                         'calculating psuedo inverse matrix.')

    # Get transformation matrix shape
    shape = list(x1.shape)
    shape[-1] += 1

    # Calculate pseudo inverse
    x1_ones = torch.ones(shape)
    x1_ones[:, :-1] = x1
    A_ones = torch.matmul(torch.linalg.pinv(x1_ones), x2.to(x1_ones.device)).T

    # Get weights and bias
    w = A_ones[..., :-1]
    b = A_ones[..., -1]

    return w, b


class StitchingLayer(nn.Module):
    def __init__(self, in_features=None, out_features=None):
        super().__init__()
        self.transform = nn.Linear(in_features, out_features)

    def init_stitch_weights_bias(self, weight, bias):
        self.transform.weight.data.copy_(weight)
        self.transform.bias.data.copy_(bias)

    def forward(self, x):
        x = self.transform(x)
        return x


class SNNet(nn.Module):
    '''
    Stitchable Neural Networks
    '''

    def __init__(self, anchors):
        super(SNNet, self).__init__()

        self.anchors = nn.ModuleList(anchors)
        stage_depths = [anc.depths for anc in self.anchors]

        total_configs = []
        self.num_stitches = []
        self.stitch_layers = nn.ModuleList()
        self.stitching_map_id = {}

        for i in range(len(self.anchors)):
            total_configs.append({
                'comb_id': [i],
                'stitch_cfgs': [],
                'stitch_layers': []
            })

        # iterate through all stages
        for i in range(4):
            
            # skip the last stage
            if i == 3:
                continue
                
            cur_depths = [stage_depths[anc_id][i] for anc_id in range(len(self.anchors))]
            stage_configs, stage_stitches = get_stitch_configs(cur_depths, i)
            self.num_stitches.append(stage_stitches)
            total_configs += stage_configs
            stage_stitching_layers = nn.ModuleList()

            for j, (num_s, comb) in enumerate(stage_stitches):
                front, end = comb
                stage_stitching_layers.append(nn.ModuleList(
                    [StitchingLayer(self.anchors[front].stage_dims[i], self.anchors[end].stage_dims[i]) for _ in range(num_s)]))
                self.stitching_map_id[f'{i}-{front}-{end}'] = j
            
            self.stitch_layers.append(stage_stitching_layers)

        self.stitch_configs = {i: cfg for i, cfg in enumerate(total_configs)}
        self.num_configs = len(total_configs)
        self.stitch_config_id = 0

    def reset_stitch_id(self, stitch_config_id):
        self.stitch_config_id = stitch_config_id


    def initialize_stitching_weights(self, x):

        anchor_features = []
        with torch.no_grad():
            for anc in self.anchors:
                anchor_features.append(anc.extract_block_features(x))

        for stage_id in range(4):
            if stage_id == 3:
                break
            stage_stitches = self.num_stitches[stage_id]

            for j, (num_s, comb) in enumerate(stage_stitches):
                front, end = comb
                stitching_dicts = defaultdict(set)
                for id, config in self.stitch_configs.items():
                    if config['comb_id'] == comb and stage_id == config['stage_id']:
                        stitching_dicts[config['stitch_layers'][0]].add(config['stitch_cfgs'][0])

                for stitch_layer_id, stitch_positions in stitching_dicts.items():
                    weight_candidates = []
                    bias_candidates = []
                    for front_id, end_id in stitch_positions:
                        front_blk_feat = anchor_features[front][stage_id][front_id]
                        end_blk_feat = anchor_features[end][stage_id][end_id - 1]
                        w, b = ps_inv(front_blk_feat, end_blk_feat)
                        weight_candidates.append(w)
                        bias_candidates.append(b)
                    weights = torch.stack(weight_candidates).mean(dim=0)
                    bias = torch.stack(bias_candidates).mean(dim=0)

                    self.stitch_layers[stage_id][j][stitch_layer_id].init_stitch_weights_bias(weights, bias)
                    print(f'Initialized Stitching Model {front} to Model {end}, Stage {stage_id}, Layer {stitch_layer_id}')


    def get_model_size(self, stitch_cfg_id):
        comb_id = self.stitch_configs[stitch_cfg_id]['comb_id']
        if len(comb_id) == 1:
            return sum(p.numel() for p in self.anchors[comb_id[0]].parameters())

        stitch_cfgs = self.stitch_configs[stitch_cfg_id]['stitch_cfgs']
        stitch_stage_id = self.stitch_configs[stitch_cfg_id]['stage_id']
        stitch_layer_ids = self.stitch_configs[stitch_cfg_id]['stitch_layers']

        cfg = stitch_cfgs[0]
        total_params = 0
        total_params += self.anchors[comb_id[0]].get_model_size_util(stage_id=stitch_stage_id, blk_id = cfg[0])

        sl_id = stitch_layer_ids[0]
        key = f'{stitch_stage_id}-{comb_id[0]}-{comb_id[1]}'
        stitch_projection_id = self.stitching_map_id[key]
        total_params += sum(p.numel() for p in self.stitch_layers[stitch_stage_id][stitch_projection_id][sl_id].parameters())


        total_params += self.anchors[comb_id[1]].get_model_size_from(stage_id=stitch_stage_id, blk_id=cfg[1])

        return total_params


    def forward(self, x):
        if self.training:
            stitch_cfg_id = np.random.randint(0, self.num_configs)
        else:
            stitch_cfg_id = self.stitch_config_id

        comb_id = self.stitch_configs[stitch_cfg_id]['comb_id']
        if len(comb_id) == 1:
            return self.anchors[comb_id[0]](x)

        stitch_cfgs = self.stitch_configs[stitch_cfg_id]['stitch_cfgs']
        stitch_stage_id = self.stitch_configs[stitch_cfg_id]['stage_id']
        stitch_layer_ids = self.stitch_configs[stitch_cfg_id]['stitch_layers']

        cfg = stitch_cfgs[0]

        x = self.anchors[comb_id[0]].forward_until(x, stage_id=stitch_stage_id, blk_id=cfg[0])

        sl_id = stitch_layer_ids[0]
        key = f'{stitch_stage_id}-{comb_id[0]}-{comb_id[1]}'
        stitch_projection_id = self.stitching_map_id[key]
        x = self.stitch_layers[stitch_stage_id][stitch_projection_id][sl_id](x)

        x = self.anchors[comb_id[1]].forward_from(x, stage_id=stitch_stage_id, blk_id=cfg[1])

        return x