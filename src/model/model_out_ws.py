# if __name__ == '__main__' and __package__ is None:
#     from os import sys
#     sys.path.append('../')
import copy
import os

import numpy as np
import torch
from torch.utils.tensorboard import SummaryWriter

from src.dataset.DataLoader import (CustomDataLoader, collate_fn_mmg, collate_fn_ws)
from src.dataset.dataset_builder import build_dataset
from src.model.SGFN_MMG.model_ws import Mmgnet
from src.utils import op_utils
from src.utils.eva_utils_acc import get_mean_recall, get_zero_shot_recall, get_head_body_tail
from src.utils.eval_utils_recall import handle_mean_recall
from process_data.relation_distribution import Result_print
from transformers import BertTokenizer

from torchsummary import summary
from thop import profile


class MMGNet():
    def __init__(self, config):
        self.config = config
        self.model_name = self.config.NAME
        self.mconfig = mconfig = config.MODEL
        self.exp = config.exp
        self.save_res = config.EVAL
        self.update_2d = config.update_2d
        
        ''' Build dataset '''
        dataset = None
        if config.MODE  == 'train':
            if config.VERBOSE: print('build train dataset')
            self.dataset_train = build_dataset(self.config,split_type='train_scans', shuffle_objs=True,
                                               multi_rel_outputs=mconfig.multi_rel_outputs,
                                               use_rgb=mconfig.USE_RGB,
                                               use_normal=mconfig.USE_NORMAL, Type = self.config.dataset.type)
            self.dataset_train.__getitem__(0)
                
        if config.MODE  == 'train' or config.MODE  == 'trace' or config.MODE  == 'eval':
            if config.VERBOSE: print('build valid dataset')
            self.dataset_valid = build_dataset(self.config,split_type='validation_scans', shuffle_objs=False, 
                                      multi_rel_outputs=mconfig.multi_rel_outputs,
                                      use_rgb=mconfig.USE_RGB,
                                      use_normal=mconfig.USE_NORMAL, Type = self.config.dataset.type)
            dataset = self.dataset_valid

        num_obj_class = len(self.dataset_valid.classNames)   
        num_rel_class = len(self.dataset_valid.relationNames)
        self.classNames = self.dataset_valid.classNames
        self.relationNames = self.dataset_valid.relationNames
        self.num_obj_class = num_obj_class
        self.num_rel_class = num_rel_class
        
        if config.MODE  == 'train' or config.MODE  == 'trace':
            self.total = self.config.total = len(self.dataset_train) // self.config.Batch_Size
            self.max_iteration = self.config.max_iteration = int(float(self.config.MAX_EPOCHES)*len(self.dataset_train) // self.config.Batch_Size)
            self.max_iteration_scheduler = self.config.max_iteration_scheduler = int(float(100)*len(self.dataset_train) // self.config.Batch_Size)
        
        ''' Build Model '''
        self.model = Mmgnet(self.config, self.dataset_valid.classNames, self.dataset_valid.relationNames).to(config.DEVICE)


        self.samples_path = os.path.join(config.PATH, self.model_name, self.exp,  'samples')
        self.results_path = os.path.join(config.PATH, self.model_name, self.exp, 'results')
        self.trace_path = os.path.join(config.PATH, self.model_name, self.exp, 'traced')
        self.writter = None
        
        if not self.config.EVAL:
            pth_log = os.path.join(config.PATH, "logs", self.model_name, self.exp)
            self.writter = SummaryWriter(pth_log)
        
        
    def load(self, best=False):
        return self.model.load(best)
        
    @torch.no_grad()
    def data_processing_train(self, items):
        obj_points, obj_2d_feats, gt_class, gt_rel_cls, edge_indices, descriptor, batch_ids, scan_id, split_id, origin_obj_points, obj_texts, tri_texts, img_pair_info, img_pair_idx = items 
        obj_points = obj_points.permute(0,2,1).contiguous()
        obj_points, obj_2d_feats, edge_indices, descriptor, batch_ids, img_pair_info, img_pair_idx, gt_class, gt_rel_cls = \
            self.cuda(obj_points, obj_2d_feats, edge_indices, descriptor, batch_ids, img_pair_info, img_pair_idx, gt_class, gt_rel_cls)
        return obj_points, obj_2d_feats, edge_indices, descriptor, batch_ids, obj_texts, tri_texts, gt_rel_cls, gt_class, img_pair_info, img_pair_idx
    
    @torch.no_grad()
    def data_processing_val(self, items):
        obj_points, obj_2d_feats, gt_class, gt_rel_cls, edge_indices, descriptor, batch_ids, scan_id, split_id, origin_obj_points, _, _, _, _ = items 
        obj_points = obj_points.permute(0,2,1).contiguous()
        obj_points, obj_2d_feats, gt_class, gt_rel_cls, edge_indices, descriptor, batch_ids = \
            self.cuda(obj_points, obj_2d_feats, gt_class, gt_rel_cls, edge_indices, descriptor, batch_ids)
        return obj_points, obj_2d_feats, gt_class, gt_rel_cls, edge_indices, descriptor, batch_ids, scan_id, split_id, origin_obj_points
          
    def train(self):
        ''' create data loader '''
        drop_last = True
        train_loader = CustomDataLoader(
            config = self.config,
            dataset=self.dataset_train,
            batch_size=self.config.Batch_Size,
            num_workers=self.config.WORKERS,
            drop_last=drop_last,
            shuffle=True,
            collate_fn=collate_fn_ws,
        )
        
        self.model.epoch = 1
        keep_training = True
        
        if self.total == 1:
            print('No training data was provided! Check \'TRAIN_FLIST\' value in the configuration file.')
            return
        
        progbar = op_utils.Progbar(self.total, width=20, stateful_metrics=['Misc/epo', 'Misc/it', 'Misc/lr'])
                
        ''' Resume data loader to the last read location '''
        loader = iter(train_loader)
                   
        if self.mconfig.use_pretrain != "":
            self.model.load_pretrain_model(self.mconfig.use_pretrain, is_freeze=True)
        
        for k, p in self.model.named_parameters():
            if p.requires_grad:
                print(f"Para {k} need grad")
        ''' Train '''
        while(keep_training):

            if self.model.epoch > self.config.MAX_EPOCHES:
                break

            print('\n\nTraining epoch: %d' % self.model.epoch)

            num_obj_text_unique = 0
            num_obj_text_unique_1 = 0
            num_all_obj_text = 0
            
            for items in loader:
                self.model.train()
                
                ''' get data '''
                obj_points, obj_2d_feats, edge_indices, descriptor, batch_ids, obj_texts, tri_texts, gt_rel_cls, gt_class, img_pair_info, img_pair_idx = self.data_processing_train(items)
                if not self.config.use_pair_info:
                    img_pair_info, img_pair_idx = None, None
                
                # 计算模型参数量
                # from torchsummary import summary
                # from thop import profile, clever_format

                # flops, params = profile(self.model, inputs=(obj_points, obj_2d_feats, edge_indices.t().contiguous(), descriptor, batch_ids, gt_class, obj_texts, tri_texts, img_pair_info, img_pair_idx, True))
                # flops, params = clever_format([flops, params], "%.3f")
                # print("FLOPs: %s" %(flops))
                # print("params: %s" %(params))


                logs = self.model.process_train(obj_points, obj_2d_feats, descriptor, edge_indices, batch_ids, obj_texts, tri_texts, gt_rel_cls, gt_class, with_log=True,
                                                weights_obj=self.dataset_train.w_cls_obj, 
                                                weights_rel=self.dataset_train.w_cls_rel,
                                                img_pair_info=img_pair_info, img_pair_idx=img_pair_idx, 
                                                ignore_none_rel = False)
                
                # for i in obj_texts:
                #     if len(np.unique(i)) == len(i):
                #         num_obj_text_unique += 1
                    
                #     if (len(np.unique(i)) + 1) == len(i):
                #         num_obj_text_unique_1 += 1
                #     num_all_obj_text += 1
                
                iteration = self.model.iteration
                logs += [
                    ("Misc/epo", int(self.model.epoch)),
                    ("Misc/it", int(iteration)),
                    ("lr", self.model.lr_scheduler.get_last_lr()[0])
                ]

                # if int(iteration) >10:
                #     break
                
                progbar.add(1, values=logs \
                            if self.config.VERBOSE else [x for x in logs if not x[0].startswith('loss')])
                if self.config.LOG_INTERVAL and iteration % self.config.LOG_INTERVAL == 0:
                    self.log(logs, iteration)
                if self.model.iteration >= self.max_iteration:
                    break

            progbar = op_utils.Progbar(self.total, width=20, stateful_metrics=['Misc/epo', 'Misc/it'])
            loader = iter(train_loader)
            self.save()

            if ('VALID_INTERVAL' in self.config and self.config.VALID_INTERVAL > 0 and self.model.epoch % self.config.VALID_INTERVAL == 0):
                print('start validation...')
                rel_acc_val = self.validation()
                self.model.eva_res = rel_acc_val
                self.save()
            
            self.model.epoch += 1
                   
    def cuda(self, *args):
        return [item.to(self.config.DEVICE) for item in args]
    
    def log(self, logs, iteration):
        # Tensorboard
        if self.writter is not None and not self.config.EVAL:
            for i in logs:
                if not i[0].startswith('Misc'):
                    self.writter.add_scalar(i[0], i[1], iteration)
                    
    def save(self):
        self.model.save()
        
    def validation(self, debug_mode = False):
        cal_recall = self.config.Cal_recall
        val_loader = CustomDataLoader(
            config = self.config,
            dataset=self.dataset_valid,
            batch_size=1,
            num_workers=self.config.WORKERS,
            drop_last=False,
            shuffle=False,
            collate_fn=collate_fn_ws
        )
       
        total = len(self.dataset_valid)
        progbar = op_utils.Progbar(total, width=20, stateful_metrics=['Misc/it'])
        
        print('===   start evaluation   ===')
        result_print = Result_print(self.config.exp, self.classNames, self.relationNames, self.config.dataset.use_rio27_dataset)
        self.model.eval()
        topk_obj_list_3d, topk_obj_list_2d, topk_rel_list, topk_triplet_list, cls_matrix_list, edge_feature_list = np.array([]), np.array([]), np.array([]), np.array([]), [], []
        sub_scores_list, obj_scores_list, rel_scores_list = [], [], []
        triplet_recall_list_wo_gc_all, relation_recall_list_wo_gc_all, triplet_recall_list_w_gc_all, relation_recall_list_w_gc_all, triplet_mean_recall_list_w_gc_all, relation_mean_recall_list_w_gc_all = [],[],[],[],[],[]
        gt_rel_list = []

        for i, items in enumerate(val_loader, 0):
            ''' get data '''
            obj_points, obj_2d_feats, gt_class, gt_rel_cls, edge_indices, descriptor, batch_ids, scan_id, split_id, origin_obj_points = self.data_processing_val(items)            
            
            with torch.no_grad():
                if cal_recall:
                    top_k_obj_3d, top_k_obj_2d, top_k_rel, tok_k_triplet, cls_matrix, sub_scores, obj_scores, rel_scores, \
                    triplet_recall_list_wo_gc, relation_recall_list_wo_gc, triplet_recall_list_w_gc, relation_recall_list_w_gc, \
                    triplet_mean_recall_list_w_gc, relation_mean_recall_list_w_gc\
                        = self.model.process_val(result_print, obj_points, obj_2d_feats, gt_class, descriptor, gt_rel_cls, edge_indices, batch_ids, scan_id, split_id, origin_obj_points, use_triplet=True, cal_recall=cal_recall)
                else:
                    top_k_obj_3d, top_k_obj_2d, top_k_rel, tok_k_triplet, cls_matrix, sub_scores, obj_scores, rel_scores, \
                        = self.model.process_val(result_print, obj_points, obj_2d_feats, gt_class, descriptor, gt_rel_cls, edge_indices, batch_ids, scan_id, split_id, origin_obj_points, use_triplet=True, cal_recall=cal_recall)
                
                        
            ''' calculate metrics '''
            gt_rel_list.append(gt_rel_cls)
            topk_obj_list_3d = np.concatenate((topk_obj_list_3d, top_k_obj_3d))
            topk_obj_list_2d = np.concatenate((topk_obj_list_2d, top_k_obj_2d))
            topk_rel_list = np.concatenate((topk_rel_list, top_k_rel))
            topk_triplet_list = np.concatenate((topk_triplet_list, tok_k_triplet))
            
            
            if cls_matrix is not None:
                cls_matrix_list.extend(cls_matrix)
                sub_scores_list.extend(sub_scores)
                obj_scores_list.extend(obj_scores)
                rel_scores_list.extend(rel_scores)
                if cal_recall:
                    triplet_recall_list_wo_gc_all.append(triplet_recall_list_wo_gc)
                    relation_recall_list_wo_gc_all.append(relation_recall_list_wo_gc)
                    triplet_recall_list_w_gc_all.append(triplet_recall_list_w_gc)
                    relation_recall_list_w_gc_all.append(relation_recall_list_w_gc)
                    triplet_mean_recall_list_w_gc_all.append(triplet_mean_recall_list_w_gc)
                    relation_mean_recall_list_w_gc_all.append(relation_mean_recall_list_w_gc)

            
            logs = [("Acc@1/obj_cls_acc_3d", (topk_obj_list_3d <= 1).sum() * 100 / len(topk_obj_list_3d)),  # 因为topk_*_list统计的是真实的class的预测概率排序位置，所以用sum的方法
                    ("Acc@5/obj_cls_acc_3d", (topk_obj_list_3d <= 5).sum() * 100 / len(topk_obj_list_3d)),
                    ("Acc@10/obj_cls_acc_3d", (topk_obj_list_3d <= 10).sum() * 100 / len(topk_obj_list_3d)),
                    ("Acc@1/obj_cls_acc_2d", (topk_obj_list_2d <= 1).sum() * 100 / len(topk_obj_list_2d)),  # 因为topk_*_list统计的是真实的class的预测概率排序位置，所以用sum的方法
                    ("Acc@5/obj_cls_acc_2d", (topk_obj_list_2d <= 5).sum() * 100 / len(topk_obj_list_2d)),
                    ("Acc@10/obj_cls_acc_2d", (topk_obj_list_2d <= 10).sum() * 100 / len(topk_obj_list_2d)),
                    ("Acc@1/rel_cls_acc", (topk_rel_list <= 1).sum() * 100 / len(topk_rel_list)),
                    ("Acc@3/rel_cls_acc", (topk_rel_list <= 3).sum() * 100 / len(topk_rel_list)),
                    ("Acc@5/rel_cls_acc", (topk_rel_list <= 5).sum() * 100 / len(topk_rel_list)),
                    ("Acc@50/triplet_acc", (topk_triplet_list <= 50).sum() * 100 / len(topk_triplet_list)),
                    ("Acc@100/triplet_acc", (topk_triplet_list <= 100).sum() * 100 / len(topk_triplet_list)),
                    # ("Recall@20/triplet", triplet_recall_list_wo_gc[0]),
                    # ("Recall@50/triplet", triplet_recall_list_wo_gc[1]),
                    # ("Recall@100/triplet", triplet_recall_list_wo_gc[2]),
                    # ("Recall@20/relation", relation_recall_list_wo_gc[0]),
                    # ("Recall@50/relation", relation_recall_list_wo_gc[1]),
                    # ("Recall@100/relation", relation_recall_list_wo_gc[2]),
                    ]

            progbar.add(1, values=logs if self.config.VERBOSE else [x for x in logs if not x[0].startswith('Loss')])

            # if i==10:
            #     break

        # result_print.print_result(self.config.exp)

        cls_matrix_list = np.stack(cls_matrix_list)
        sub_scores_list = np.stack(sub_scores_list)
        obj_scores_list = np.stack(obj_scores_list)
        rel_scores_list = np.stack(rel_scores_list)
        mean_recall = get_mean_recall(topk_triplet_list, cls_matrix_list)
        zero_shot_recall, non_zero_shot_recall, all_zero_shot_recall = get_zero_shot_recall(topk_triplet_list, cls_matrix_list, self.dataset_valid.classNames, self.dataset_valid.relationNames, self.config.dataset.use_rio27_dataset)
        rel_head_mean, rel_body_mean, rel_tail_mean = get_head_body_tail(cls_matrix_list, topk_rel_list, self.dataset_valid.relationNames)    


        if self.model.config.EVAL:
            save_path = os.path.join(self.config.PATH, "results", self.model_name, self.exp)
            os.makedirs(save_path, exist_ok=True)
            np.save(os.path.join(save_path,'topk_pred_list.npy'), topk_rel_list )
            np.save(os.path.join(save_path,'topk_triplet_list.npy'), topk_triplet_list)
            np.save(os.path.join(save_path,'cls_matrix_list.npy'), cls_matrix_list)
            np.save(os.path.join(save_path,'sub_scores_list.npy'), sub_scores_list)
            np.save(os.path.join(save_path,'obj_scores_list.npy'), obj_scores_list)
            np.save(os.path.join(save_path,'rel_scores_list.npy'), rel_scores_list)
            f_in = open(os.path.join(save_path, 'result.txt'), 'a+')
        else:
            f_in = None   

        


        # save_path = os.path.join(self.config.PATH, "results", self.model_name, self.exp, "val_result")
        # os.makedirs(save_path, exist_ok=True)
        # np.save(os.path.join(save_path,'topk_pred_list.npy'), topk_rel_list )
        # np.save(os.path.join(save_path,'topk_triplet_list.npy'), topk_triplet_list)
        # np.save(os.path.join(save_path,'cls_matrix_list.npy'), cls_matrix_list)
        # np.save(os.path.join(save_path,'sub_scores_list.npy'), sub_scores_list)
        # np.save(os.path.join(save_path,'obj_scores_list.npy'), obj_scores_list)
        # np.save(os.path.join(save_path,'rel_scores_list.npy'), rel_scores_list)
        # f_in = open(os.path.join(save_path, 'result.txt'), 'a+')




        obj_acc_1_3d = (topk_obj_list_3d <= 1).sum() * 100 / len(topk_obj_list_3d)
        obj_acc_5_3d = (topk_obj_list_3d <= 5).sum() * 100 / len(topk_obj_list_3d)
        obj_acc_10_3d = (topk_obj_list_3d <= 10).sum() * 100 / len(topk_obj_list_3d)
        obj_acc_1_2d = (topk_obj_list_2d <= 1).sum() * 100 / len(topk_obj_list_2d)
        obj_acc_5_2d = (topk_obj_list_2d <= 5).sum() * 100 / len(topk_obj_list_2d)
        obj_acc_10_2d = (topk_obj_list_2d <= 10).sum() * 100 / len(topk_obj_list_2d)
        rel_acc_1 = (topk_rel_list <= 1).sum() * 100 / len(topk_rel_list)
        rel_acc_3 = (topk_rel_list <= 3).sum() * 100 / len(topk_rel_list)
        rel_acc_5 = (topk_rel_list <= 5).sum() * 100 / len(topk_rel_list)
        triplet_acc_50 = (topk_triplet_list <= 50).sum() * 100 / len(topk_triplet_list)
        triplet_acc_100 = (topk_triplet_list <= 100).sum() * 100 / len(topk_triplet_list)

        rel_acc_mean_1, rel_acc_mean_3, rel_acc_mean_5 = self.compute_mean_predicate(cls_matrix_list, topk_rel_list)

        print("--------------------------------------------", file=f_in)
        print(f"Eval: 2d obj Acc@1  : {obj_acc_1_2d}", file=f_in)   
        print(f"Eval: 2d obj Acc@5  : {obj_acc_5_2d}", file=f_in)  
        print(f"Eval: 2d obj Acc@10 : {obj_acc_10_2d}", file=f_in)  
        print(f"Eval: 3d obj Acc@1  : {obj_acc_1_3d}", file=f_in)   
        print(f"Eval: 3d obj Acc@5  : {obj_acc_5_3d}", file=f_in)  
        print(f"Eval: 3d obj Acc@10 : {obj_acc_10_3d}", file=f_in)  
        print(f"Eval: 3d rel Acc@1  : {rel_acc_1}", file=f_in) 
        print(f"Eval: 3d mean rel Acc@1  : {rel_acc_mean_1}", file=f_in)   
        print(f"Eval: 3d rel Acc@3  : {rel_acc_3}", file=f_in)   
        print(f"Eval: 3d mean rel Acc@3  : {rel_acc_mean_3}", file=f_in) 
        print(f"Eval: 3d rel Acc@5  : {rel_acc_5}", file=f_in)
        print(f"Eval: 3d mean rel Acc@5  : {rel_acc_mean_5}", file=f_in) 
        print(f"Eval: 3d triplet Acc@50 : {triplet_acc_50}", file=f_in)
        print(f"Eval: 3d triplet Acc@100 : {triplet_acc_100}", file=f_in)
        print(f"Eval: 3d mean recall@50 : {mean_recall[0]}", file=f_in)
        print(f"Eval: 3d mean recall@100 : {mean_recall[1]}", file=f_in)
        print(f"Eval: 3d zero-shot recall@50 : {zero_shot_recall[0]}", file=f_in)
        print(f"Eval: 3d zero-shot recall@100: {zero_shot_recall[1]}", file=f_in)
        print(f"Eval: 3d non-zero-shot recall@50 : {non_zero_shot_recall[0]}", file=f_in)
        print(f"Eval: 3d non-zero-shot recall@100: {non_zero_shot_recall[1]}", file=f_in)
        print(f"Eval: 3d all-zero-shot recall@50 : {all_zero_shot_recall[0]}", file=f_in)
        print(f"Eval: 3d all-zero-shot recall@100: {all_zero_shot_recall[1]}", file=f_in)
        print(f"Eval: 3d head mean Acc@1: {rel_head_mean[0]}", file=f_in)
        print(f"Eval: 3d head mean Acc@3: {rel_head_mean[1]}", file=f_in)
        print(f"Eval: 3d head mean Acc@5: {rel_head_mean[2]}", file=f_in)
        print(f"Eval: 3d body mean Acc@1: {rel_body_mean[0]}", file=f_in)
        print(f"Eval: 3d body mean Acc@3: {rel_body_mean[1]}", file=f_in)
        print(f"Eval: 3d body mean Acc@5: {rel_body_mean[2]}", file=f_in)
        print(f"Eval: 3d tail mean Acc@1: {rel_tail_mean[0]}", file=f_in)
        print(f"Eval: 3d tail mean Acc@3: {rel_tail_mean[1]}", file=f_in)
        print(f"Eval: 3d tail mean Acc@5: {rel_tail_mean[2]}", file=f_in)
        
        if cal_recall:
            triplet_mean_recall_list_w_gc_all = handle_mean_recall(triplet_mean_recall_list_w_gc_all)
            relation_mean_recall_list_w_gc_all = handle_mean_recall(relation_mean_recall_list_w_gc_all)

            L = len(triplet_recall_list_wo_gc_all)
            triplet_recall_list_wo_gc_all, relation_recall_list_wo_gc_all, triplet_recall_list_w_gc_all, relation_recall_list_w_gc_all, triplet_mean_recall_list_w_gc_all, relation_mean_recall_list_w_gc_all
            
            triplet_recall_list_wo_gc_all = np.array(triplet_recall_list_wo_gc_all).sum(0) / L * 100
            relation_recall_list_wo_gc_all = np.array(relation_recall_list_wo_gc_all).sum(0) / L * 100
            triplet_recall_list_w_gc_all = np.array(triplet_recall_list_w_gc_all).sum(0) / L * 100
            relation_recall_list_w_gc_all = np.array(relation_recall_list_w_gc_all).sum(0) / L * 100
            triplet_mean_recall_list_w_gc_all = triplet_mean_recall_list_w_gc_all* 100
            relation_mean_recall_list_w_gc_all = relation_mean_recall_list_w_gc_all* 100
            
            print()
            print(f"Eval: SGCls triplet recall without GC@20 : {triplet_recall_list_wo_gc_all[0]}", file=f_in)
            print(f"Eval: SGCls triplet recall without GC@50 : {triplet_recall_list_wo_gc_all[1]}", file=f_in)
            print(f"Eval: SGCls triplet recall without GC@100 : {triplet_recall_list_wo_gc_all[2]}", file=f_in)
            print(f"Eval: PredCls relation recall without GC@20 : {relation_recall_list_wo_gc_all[0]}", file=f_in)
            print(f"Eval: PredCls relation recall without GC@50 : {relation_recall_list_wo_gc_all[1]}", file=f_in)
            print(f"Eval: PredCls relation recall without GC@100 : {relation_recall_list_wo_gc_all[2]}", file=f_in)

            print(f"Eval: SGCls triplet recall with GC@20 : {triplet_recall_list_w_gc_all[0]}", file=f_in)
            print(f"Eval: SGCls triplet recall with GC@50 : {triplet_recall_list_w_gc_all[1]}", file=f_in)
            print(f"Eval: SGCls triplet recall with GC@100 : {triplet_recall_list_w_gc_all[2]}", file=f_in)
            print(f"Eval: PredCls relation recall with GC@20 : {relation_recall_list_w_gc_all[0]}", file=f_in)
            print(f"Eval: PredCls relation recall with GC@50 : {relation_recall_list_w_gc_all[1]}", file=f_in)
            print(f"Eval: PredCls relation recall with GC@100 : {relation_recall_list_w_gc_all[2]}", file=f_in)

            print(f"Eval: SGCls triplet mean recall with GC@20 : {triplet_mean_recall_list_w_gc_all[0]}", file=f_in)
            print(f"Eval: SGCls triplet mean recall with GC@50 : {triplet_mean_recall_list_w_gc_all[1]}", file=f_in)
            print(f"Eval: SGCls triplet mean recall with GC@100 : {triplet_mean_recall_list_w_gc_all[2]}", file=f_in)
            print(f"Eval: PredCls relation mean recall with GC@20 : {relation_mean_recall_list_w_gc_all[0]}", file=f_in)
            print(f"Eval: PredCls relation mean recall with GC@50 : {relation_mean_recall_list_w_gc_all[1]}", file=f_in)
            print(f"Eval: PredCls relation mean recall with GC@100 : {relation_mean_recall_list_w_gc_all[2]}", file=f_in)


        if self.model.config.EVAL:
            f_in.close()
        
        logs = [("Acc@1/obj_cls_acc_2d", obj_acc_1_2d),
                ("Acc@5/obj_cls_acc_2d", obj_acc_5_2d),
                ("Acc@10/obj_cls_acc_2d", obj_acc_10_2d),
                ("Acc@1/obj_cls_acc_3d", obj_acc_1_3d),
                ("Acc@5/obj_cls_acc_3d", obj_acc_5_3d),
                ("Acc@10/obj_cls_acc_3d", obj_acc_10_3d),
                ("Acc@1/rel_cls_acc", rel_acc_1),
                ("Acc@1/rel_cls_acc_mean", rel_acc_mean_1),
                ("Acc@3/rel_cls_acc", rel_acc_3),
                ("Acc@3/rel_cls_acc_mean", rel_acc_mean_3),
                ("Acc@5/rel_cls_acc", rel_acc_5),
                ("Acc@5/rel_cls_acc_mean", rel_acc_mean_5),
                ("Acc@50/triplet_acc", triplet_acc_50),
                ("Acc@100/triplet_acc", triplet_acc_100),
                ("mean_recall@50", mean_recall[0]),
                ("mean_recall@100", mean_recall[1]),
                ("zero_shot_recall@50", zero_shot_recall[0]),
                ("zero_shot_recall@100", zero_shot_recall[1]),
                ("non_zero_shot_recall@50", non_zero_shot_recall[0]),
                ("non_zero_shot_recall@100", non_zero_shot_recall[1]),
                ("all_zero_shot_recall@50", all_zero_shot_recall[0]),
                ("all_zero_shot_recall@100", all_zero_shot_recall[1])
                ]
        
        self.log(logs, self.model.iteration)
        return mean_recall[0]
    
    def compute_mean_predicate(self, cls_matrix_list, topk_pred_list):
        cls_dict = {}
        for i in range(26):
            cls_dict[i] = []
        
        for idx, j in enumerate(cls_matrix_list):
            if j[-1] != -1:
                cls_dict[j[-1]].append(topk_pred_list[idx])  # cls_dict里面存放的是真实的类别概率在预测的所有类别的概率中的排序
        
        predicate_mean_1, predicate_mean_3, predicate_mean_5 = [], [], []
        for i in range(26):
            l = len(cls_dict[i])
            if l > 0:
                m_1 = (np.array(cls_dict[i]) <= 1).sum() / len(cls_dict[i])  # 
                m_3 = (np.array(cls_dict[i]) <= 3).sum() / len(cls_dict[i])
                m_5 = (np.array(cls_dict[i]) <= 5).sum() / len(cls_dict[i])
                predicate_mean_1.append(m_1)
                predicate_mean_3.append(m_3)
                predicate_mean_5.append(m_5) 
           
        predicate_mean_1 = np.mean(predicate_mean_1)
        predicate_mean_3 = np.mean(predicate_mean_3)
        predicate_mean_5 = np.mean(predicate_mean_5)

        return predicate_mean_1 * 100, predicate_mean_3 * 100, predicate_mean_5 * 100
