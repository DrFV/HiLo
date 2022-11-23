# Copyright (c) OpenMMLab. All rights reserved.
from collections import defaultdict
import argparse
import os
import os.path as osp
import time
import warnings
import pprint
import copy
import numpy as np

import mmcv
import torch
from grade import save_results
from mmcv import Config, DictAction
from mmcv.cnn import fuse_conv_bn
from mmcv.parallel import MMDataParallel, MMDistributedDataParallel
from mmcv.runner import (get_dist_info, init_dist, load_checkpoint,
                         wrap_fp16_model)
from mmdet.apis import multi_gpu_test, single_gpu_test
from mmdet.datasets import build_dataloader, replace_ImageToTensor
from mmdet.models import build_detector

from openpsg.datasets import build_dataset
from openpsg.models.relation_heads.approaches import Result
from grade import save_results

if (os.getenv('SAVE_PREDICT', 'false').lower() == 'true') or \
        (os.getenv('MERGE_PREDICT', 'false').lower() == 'true'):
    from mmdet.core import encode_mask_results
    from mmcv.parallel import DataContainer as DC


def parse_args():
    parser = argparse.ArgumentParser(
        description='MMDet test (and eval) a model')
    parser.add_argument('config', help='test config file path')
    parser.add_argument('checkpoint', help='checkpoint file')
    parser.add_argument(
        '--work-dir',
        help='the directory to save the file containing evaluation metrics')
    parser.add_argument('--out', help='output result file in pickle format')
    parser.add_argument(
        '--fuse-conv-bn',
        action='store_true',
        help='Whether to fuse conv and bn, this will slightly increase'
        'the inference speed')
    parser.add_argument(
        '--format-only',
        action='store_true',
        help='Format the output results without perform evaluation. It is'
        'useful when you want to format the result to a specific format and '
        'submit it to the test server')
    parser.add_argument(
        '--eval',
        type=str,
        nargs='+',
        help='evaluation metrics, which depends on the dataset, e.g., "bbox",'
        ' "segm", "proposal" for COCO, and "mAP", "recall" for PASCAL VOC')
    parser.add_argument('--show', action='store_true', help='show results')
    parser.add_argument('--show-dir',
                        help='directory where painted images will be saved')
    parser.add_argument('--show-score-thr',
                        type=float,
                        default=0.3,
                        help='score threshold (default: 0.3)')
    parser.add_argument('--gpu-collect',
                        action='store_true',
                        help='whether to use gpu to collect results.')
    parser.add_argument(
        '--tmpdir',
        help='tmp directory used for collecting results from multiple '
        'workers, available when gpu-collect is not specified')
    parser.add_argument(
        '--cfg-options',
        nargs='+',
        action=DictAction,
        help='override some settings in the used config, the key-value pair '
        'in xxx=yyy format will be merged into config file. If the value to '
        'be overwritten is a list, it should be like key="[a,b]" or key=a,b '
        'It also allows nested list/tuple values, e.g. key="[(a,b),(c,d)]" '
        'Note that the quotation marks are necessary and that no white space '
        'is allowed.')
    parser.add_argument(
        '--options',
        nargs='+',
        action=DictAction,
        help='custom options for evaluation, the key-value pair in xxx=yyy '
        'format will be kwargs for dataset.evaluate() function (deprecate), '
        'change to --eval-options instead.')
    parser.add_argument(
        '--eval-options',
        nargs='+',
        action=DictAction,
        help='custom options for evaluation, the key-value pair in xxx=yyy '
        'format will be kwargs for dataset.evaluate() function')
    parser.add_argument('--launcher',
                        choices=['none', 'pytorch', 'slurm', 'mpi'],
                        default='none',
                        help='job launcher')
    parser.add_argument(
        '--submit',
        action='store_true',
        help='save output to a json file and save the panoptic mask as a png image into a folder for grading purpose'
    )

    parser.add_argument('--local_rank', type=int, default=0)
    args = parser.parse_args()
    if 'LOCAL_RANK' not in os.environ:
        os.environ['LOCAL_RANK'] = str(args.local_rank)

    if args.options and args.eval_options:
        raise ValueError(
            '--options and --eval-options cannot be both '
            'specified, --options is deprecated in favor of --eval-options')
    if args.options:
        warnings.warn('--options is deprecated in favor of --eval-options')
        args.eval_options = args.options
    return args


def main():
    args = parse_args()

    assert args.out or args.eval or args.format_only or args.show \
        or args.show_dir or args.submit, \
        ('Please specify at least one operation (save/eval/format/show the '
         'results / save the results) with the argument "--out", "--eval"'
         ', "--format-only", "--show" or "--show-dir"')

    if args.eval and args.format_only:
        raise ValueError('--eval and --format_only cannot be both specified')

    if args.out is not None and not args.out.endswith(('.pkl', '.pickle')):
        raise ValueError('The output file must be a pkl file.')

    cfg = Config.fromfile(args.config)
    if args.cfg_options is not None:
        cfg.merge_from_dict(args.cfg_options)
    # set cudnn_benchmark
    if cfg.get('cudnn_benchmark', False):
        torch.backends.cudnn.benchmark = True

    cfg.model.pretrained = None
    if cfg.model.get('neck'):
        if isinstance(cfg.model.neck, list):
            for neck_cfg in cfg.model.neck:
                if neck_cfg.get('rfp_backbone'):
                    if neck_cfg.rfp_backbone.get('pretrained'):
                        neck_cfg.rfp_backbone.pretrained = None
        elif cfg.model.neck.get('rfp_backbone'):
            if cfg.model.neck.rfp_backbone.get('pretrained'):
                cfg.model.neck.rfp_backbone.pretrained = None

    # in case the test dataset is concatenated
    samples_per_gpu = 1
    if isinstance(cfg.data.test, dict):
        cfg.data.test.test_mode = True
        samples_per_gpu = cfg.data.test.pop('samples_per_gpu', 1)
        if samples_per_gpu > 1:
            # Replace 'ImageToTensor' to 'DefaultFormatBundle'
            cfg.data.test.pipeline = replace_ImageToTensor(
                cfg.data.test.pipeline)
    elif isinstance(cfg.data.test, list):
        for ds_cfg in cfg.data.test:
            ds_cfg.test_mode = True
        samples_per_gpu = max(
            [ds_cfg.pop('samples_per_gpu', 1) for ds_cfg in cfg.data.test])
        if samples_per_gpu > 1:
            for ds_cfg in cfg.data.test:
                ds_cfg.pipeline = replace_ImageToTensor(ds_cfg.pipeline)

    # init distributed env first, since logger depends on the dist info.
    if args.launcher == 'none':
        distributed = False
    else:
        distributed = True
        init_dist(args.launcher, **cfg.dist_params)

    rank, _ = get_dist_info()
    # allows not to create
    if args.work_dir is not None and rank == 0:
        mmcv.mkdir_or_exist(osp.abspath(args.work_dir))
        timestamp = time.strftime('%Y%m%d_%H%M%S', time.localtime())
        json_file = osp.join(args.work_dir, f'eval_{timestamp}.json')

    # build the dataloader
    dataset = build_dataset(cfg.data.test)
    data_loader = build_dataloader(dataset,
                                   samples_per_gpu=samples_per_gpu,
                                   workers_per_gpu=cfg.data.workers_per_gpu,
                                   dist=distributed,
                                   shuffle=False)

    # build the model and load checkpoint
    if not (os.getenv('SAVE_PREDICT', 'false').lower() == 'true') and \
            not (os.getenv('MERGE_PREDICT', 'false').lower() == 'true'):
        cfg.model.train_cfg = None
    model = build_detector(cfg.model, test_cfg=cfg.get('test_cfg'))
    fp16_cfg = cfg.get('fp16', None)
    if fp16_cfg is not None:
        wrap_fp16_model(model)
    checkpoint = load_checkpoint(model, args.checkpoint, map_location='cpu')
    if args.fuse_conv_bn:
        model = fuse_conv_bn(model)
    # old versions did not save class info in checkpoints, this walkaround is
    # for backward compatibility
    if 'CLASSES' in checkpoint.get('meta', {}):
        model.CLASSES = checkpoint['meta']['CLASSES']
    else:
        model.CLASSES = dataset.CLASSES
    # NOTE:
    if hasattr(dataset, 'PREDICATES'):
        model.PREDICATES = dataset.PREDICATES

    if not distributed:
        model = MMDataParallel(model, device_ids=[0])
        start_time = time.time()
        outputs = single_gpu_test(model, data_loader, args.show, args.show_dir,
                                  args.show_score_thr)
        '''
        # ##### code to test, add labels to test process.
        model.eval()
        outputs = []
        for i, data in enumerate(data_loader):
            print(i)
            with torch.no_grad():
                device = data['gt_labels'][0].data[0][0].device
                gt_masks = data['gt_masks'][0].data[0][0].to_tensor(
                    torch.uint8, device)
                data['gt_masks'] = [DC([[gt_masks]])]
                result = model(return_loss=False, rescale=True, **data)
            # encode mask results
            if isinstance(result[0], tuple):
                result = [(bbox_results, encode_mask_results(mask_results))
                          for bbox_results, mask_results in result]
            # This logic is only used in panoptic segmentation test.
            elif isinstance(result[0], dict) and 'ins_results' in result[0]:
                for j in range(len(result)):
                    bbox_results, mask_results = result[j]['ins_results']
                    result[j]['ins_results'] = (bbox_results,
                                                encode_mask_results(mask_results))
            outputs.extend(result)
        # ##### code to test, add labels to test process.
        '''

        duration = time.time() - start_time
        mean_duration = duration / len(data_loader)
        print('\n inference time:', flush=True)
        print(mean_duration, flush=True)
    else:
        model = MMDistributedDataParallel(
            model.cuda(),
            device_ids=[torch.cuda.current_device()],
            broadcast_buffers=False)
        outputs = multi_gpu_test(model, data_loader, args.tmpdir,
                                 args.gpu_collect)

    if args.submit:
        save_results(outputs)

    rank, _ = get_dist_info()
    if rank == 0:
        if args.out:
            print(f'\nwriting results to {args.out}')
            mmcv.dump(outputs, args.out)
        kwargs = {} if args.eval_options is None else args.eval_options
        if args.format_only:
            dataset.format_results(outputs, **kwargs)
        if args.eval:
            eval_kwargs = cfg.get('evaluation', {}).copy()
            # hard-code way to remove EvalHook args
            for key in ['interval', 'tmpdir', 'start', 'gpu_collect', 'save_best',
                        'rule', 'dynamic_intervals']:
                eval_kwargs.pop(key, None)

            if args.eval[0] == 'sgdet_PQ':
                eval_kwargs['metric'] = 'sgdet'
                metric_sgdet = dataset.evaluate(outputs, **eval_kwargs)
                eval_kwargs['metric'] = 'PQ'
                metric_pq = dataset.evaluate(outputs, **eval_kwargs)
                final_score = metric_sgdet['sgdet_recall_R_20'] * 0.3 + \
                    metric_sgdet['sgdet_mean_recall_mR_20'] * 0.6 + \
                    metric_pq['PQ'] * 0.01 * 0.1
                metric_results = dict()
                metric_results['sgdet_recall_R_20'] = metric_sgdet['sgdet_recall_R_20']
                metric_results['sgdet_recall_R_50'] = metric_sgdet['sgdet_recall_R_50']
                metric_results['sgdet_recall_R_100'] = metric_sgdet['sgdet_recall_R_100']
                metric_results['sgdet_mean_recall_mR_20'] = metric_sgdet['sgdet_mean_recall_mR_20']
                metric_results['sgdet_mean_recall_mR_50'] = metric_sgdet['sgdet_mean_recall_mR_50']
                metric_results['sgdet_mean_recall_mR_100'] = metric_sgdet['sgdet_mean_recall_mR_100']
                metric_results['PQ'] = metric_pq['PQ']
                metric_results['final_score'] = final_score
            else:
                eval_kwargs.update(dict(metric=args.eval, **kwargs))
                metric = dataset.evaluate(outputs, **eval_kwargs)
                metric_results = metric

            pprint.pprint(metric_results)


def dedup_triplets_based_on_iou(sub_labels, obj_labels, rel_labels, sub_masks, obj_masks):
    relation_classes = defaultdict(lambda: [])
    for k, (s_l, o_l, r_l) in enumerate(zip(sub_labels, obj_labels, rel_labels)):
        relation_classes[(s_l, o_l, r_l)].append(k)
    h, w = sub_masks.shape[-2:]
    flatten_sub_masks = sub_masks.reshape((-1, h*w))
    flatten_obj_masks = obj_masks.reshape((-1, h*w))

    def _dedup_triplets(triplets_ids, sub_masks, obj_masks, keep_tri):
        while len(triplets_ids) > 1:
            base_s_mask = sub_masks[triplets_ids[0:1]]
            base_o_mask = obj_masks[triplets_ids[0:1]]
            other_s_mask = sub_masks[triplets_ids[1:]]
            other_o_mask = obj_masks[triplets_ids[1:]]
            # calculate ious
            s_ious = np.matmul(base_s_mask.astype(np.int64), other_s_mask.transpose(
                1, 0).astype(np.int64)) / ((base_s_mask+other_s_mask) > 0).sum(-1)
            o_ious = np.matmul(base_o_mask.astype(np.int64), other_o_mask.transpose(
                1, 0).astype(np.int64)) / ((base_o_mask+other_o_mask) > 0).sum(-1)
            ids_left = []
            for s_iou, o_iou, other_id in zip(s_ious[0], o_ious[0], triplets_ids[1:]):
                if (s_iou > 0.99999) & (o_iou > 0.99999):
                    keep_tri[other_id] = False
                else:
                    ids_left.append(other_id)
            triplets_ids = ids_left
        return keep_tri

    keep_tri = np.ones_like(rel_labels)
    for triplets_ids in relation_classes.values():
        if len(triplets_ids) > 1:
            keep_tri = _dedup_triplets(
                triplets_ids, flatten_sub_masks, flatten_obj_masks, keep_tri)
    return keep_tri


def merge_results(result1, result2):
    # when eval_pan_rels is true, it is more complicated to merge two results.
    # because we should merge two pan_seg into one.
    assert os.getenv('EVAL_PAN_RELS', 'true').lower() != 'true'
    result1 = copy.deepcopy(result1)
    result2 = copy.deepcopy(result2)
    assert isinstance(result1, Result)
    assert isinstance(result2, Result)

    # 1. parse result1
    bboxes1 = result1.refine_bboxes
    labels1 = result1.labels
    rel_pairs1 = result1.rel_pair_idxes
    rel_dists1 = result1.rel_dists
    rel_labels1 = result1.rel_labels
    rel_scores1 = result1.rel_scores
    masks1 = result1.masks
    pan_seg1 = result1.pan_results
    num1 = rel_pairs1.shape[0]

    # 2. parse result2
    bboxes2 = result2.refine_bboxes
    labels2 = result2.labels
    rel_pairs2 = result2.rel_pair_idxes
    # after merging, rel_pairs2 should be shifted with num1
    rel_pairs2 += num1
    rel_dists2 = result2.rel_dists
    rel_labels2 = result2.rel_labels
    rel_scores2 = result2.rel_scores
    masks2 = result2.masks
    pan_seg2 = result2.pan_results
    num2 = rel_pairs2.shape[0]

    # 3. re-arrange based on rel_scores, output rel_idxes
    rel_scores_all = np.concatenate((rel_scores1, rel_scores2), axis=0)
    rel_idxes = np.argsort(rel_scores_all, axis=0)[::-1]

    # 4. reshape result to (n, ...)
    bboxes1 = bboxes1.reshape((num1, 2, 5))
    labels1 = labels1.reshape((num1, 2))
    h, w = masks1.shape[-2:]
    masks1 = masks1.reshape((num1, 2, h, w))

    bboxes2 = bboxes2.reshape((num2, 2, 5))
    labels2 = labels2.reshape((num2, 2))
    h, w = masks2.shape[-2:]
    masks2 = masks2.reshape((num2, 2, h, w))

    # 5. concatenate result1 and result2
    bboxes_all = np.concatenate((bboxes1, bboxes2), axis=0)
    labels_all = np.concatenate((labels1, labels2), axis=0)
    rel_dists_all = np.concatenate((rel_dists1, rel_dists2), axis=0)
    rel_labels_all = np.concatenate((rel_labels1, rel_labels2), axis=0)
    masks_all = np.concatenate((masks1, masks2), axis=0)

    # 6. re-arrange based on rel_idxes
    bboxes = bboxes_all[rel_idxes]
    labels = labels_all[rel_idxes]
    rel_dists = rel_dists_all[rel_idxes]
    rel_labels = rel_labels_all[rel_idxes]
    rel_scores = rel_scores_all[rel_idxes]
    masks = masks_all[rel_idxes]

    # 7. dedup
    keep_tri = dedup_triplets_based_on_iou(
        labels[:, 0], labels[:, 1], rel_labels, masks[:, 0], masks[:, 1])
    rel_pairs = np.array([i for i in range(keep_tri.sum()*2)],
                         dtype=np.int64).reshape(2, -1).T
    keep_tri = keep_tri.astype(np.bool8)
    bboxes = bboxes[keep_tri]
    labels = labels[keep_tri]
    rel_dists = rel_dists[keep_tri]
    rel_labels = rel_labels[keep_tri]
    rel_scores = rel_scores[keep_tri]
    masks = masks[keep_tri]

    # 8. reshape bboxes, labels and masks to (n*2, ...)
    bboxes = bboxes.reshape((-1, 5))
    labels = labels.reshape((-1))
    masks = masks.reshape((-1, h, w))

    # 9. construct merge result
    merge_result = Result(refine_bboxes=bboxes,
                          labels=labels,
                          formatted_masks=dict(pan_results=pan_seg1),
                          rel_pair_idxes=rel_pairs,
                          rel_dists=rel_dists,
                          rel_labels=rel_labels,
                          rel_scores=rel_scores,
                          pan_results=pan_seg1,
                          masks=masks)
    return merge_result


if __name__ == '__main__':
    main()
