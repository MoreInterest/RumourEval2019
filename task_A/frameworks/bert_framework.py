import csv
import json
import logging
import math
import os
import socket
import time
from collections import Counter, defaultdict

import torch
import torch.nn.functional as F
from pytorch_pretrained_bert import BertAdam, BertTokenizer
from sklearn import metrics
from torch.nn.modules.loss import _Loss
from torchtext.data import BucketIterator, Iterator
from tqdm import tqdm

from task_A.datasets.RumourEvalDataset_BERT import RumourEval2019Dataset_BERTTriplets_with_Tags
from task_A.frameworks.base_framework import Base_Framework
from task_A.frameworks.self_att_with_bert_tokenizing import SelfAtt_BertTokenizing_Framework
from utils import count_parameters, get_timestamp

map_stance_label_to_s = {
    0: "support",
    1: "comment",
    2: "deny",
    3: "query"
}
map_s_to_label_stance = {y: x for x, y in map_stance_label_to_s.items()}


class BERT_Framework(Base_Framework):
    def __init__(self, config: dict):
        super().__init__(config)
        self.save_treshold = 0.52
        self.tokenizer = BertTokenizer.from_pretrained("bert-base-uncased", cache_dir="./.BERTcache",
                                                       do_lower_case=True)

    def run_epoch(self, model, lossfunction, optimizer, train_iter, config, verbose=False):
        total_batches = len(train_iter.data()) // train_iter.batch_size
        if verbose:
            pbar = tqdm(total=total_batches)
        examples_so_far = 0
        train_loss = 0
        total_correct = 0

        update_ratio = config["hyperparameters"]["true_batch_size"] // config["hyperparameters"]["batch_size"]
        optimizer.zero_grad()
        updated = False
        for i, batch in enumerate(train_iter):
            updated = False
            pred_logits = model(batch)

            loss = lossfunction(pred_logits, batch.stance_label) / update_ratio
            loss.backward()

            if (i + 1) % update_ratio == 0:
                optimizer.step()
                optimizer.zero_grad()
                updated = True

            total_correct += self.calculate_correct(pred_logits, batch.stance_label)
            examples_so_far += len(batch.stance_label)
            train_loss += loss.item()
            if verbose:
                pbar.set_description(
                    f"train loss:"
                    f" {train_loss / (i + 1):.4f}, train acc: {total_correct / examples_so_far:.4f}")
                pbar.update(1)

        if not updated:
            optimizer.step()
            optimizer.zero_grad()

        return train_loss / total_batches, total_correct / examples_so_far

    def predict(self, fname: str, model: torch.nn.Module, dev_iter: Iterator, task="subtaskaenglish"):
        train_flag = model.training
        model.eval()
        answers = {"subtaskaenglish": dict(),
                   "subtaskbenglish": dict(),
                   "subtaskadanish": dict(),
                   "subtaskbdanish": dict(),
                   "subtaskarussian": dict(),
                   "subtaskbrussian": dict()
                   }
        for i, batch in enumerate(dev_iter):
            pred_logits = model(batch)
            preds = torch.argmax(pred_logits, dim=1)
            preds = list(preds.cpu().numpy())

            for ix, p in enumerate(preds):
                answers[task][batch.tweet_id[ix]] = map_stance_label_to_s[p.item()]

        with open(fname, "w") as  answer_file:
            json.dump(answers, answer_file)
        if train_flag:
            model.train()
        logging.info(f"Writing results into {fname}")

    def train(self, modelfunc):
        config = self.config

        fields = RumourEval2019Dataset_BERTTriplets_with_Tags.prepare_fields_for_text()
        train_data = RumourEval2019Dataset_BERTTriplets_with_Tags(config["train_data"], fields, self.tokenizer,
                                                                  max_length=config["hyperparameters"]["max_length"])
        dev_data = RumourEval2019Dataset_BERTTriplets_with_Tags(config["dev_data"], fields, self.tokenizer,
                                                                max_length=config["hyperparameters"]["max_length"])

        # torch.manual_seed(5246727901370826861 & ((1 << 63) - 1))
        # torch.manual_seed(40)

        # 84.1077



        device = torch.device("cuda:0" if config['cuda'] and
                                          torch.cuda.is_available() else "cpu")

        create_iter = lambda data: BucketIterator(data, #sort_key=lambda x: -len(x.text), sort=True,
                                                  shuffle=True,
                                                  batch_size=config["hyperparameters"]["batch_size"], repeat=False,
                                                  device=device)
        train_iter = create_iter(train_data)
        dev_iter = create_iter(dev_data)

        logging.info(f"Train examples: {len(train_data.examples)}\nValidation examples: {len(dev_data.examples)}")

        # bert-base-uncased
        # bert-large-uncased,
        # bert-base-multilingual-cased
        # pretrained_model = torch.load(
        #     "saved/dev/checkpoint_<class 'task_A.frameworks.bert_framework.BERT_Framework'>_F1_0.53206_2019-01-16_18:28.pt").to(
        #     device)
        # model = modelfunc.from_pretrained("bert-base-uncased", cache_dir="./.BERTcache",
        #                                   state_dict=pretrained_model.state_dict()
        #                                   ).to(device)
        # pretrained_model = None
        model = modelfunc.from_pretrained("bert-base-uncased", cache_dir="./.BERTcache").to(device)
        logging.info(f"Model has {count_parameters(model)} trainable parameters.")
        logging.info(f"Manual seed {torch.initial_seed()}")
        optimizer = BertAdam(filter(lambda p: p.requires_grad, model.parameters()),
                             # t_total = 1000,warmup=0.5,
                             lr=config["hyperparameters"]["learning_rate"])

        # No BERT training
        # optimizer = BertAdam([p[1]
        #                       for p in model.named_parameters()
        #                       if p[1].requires_grad and not p[0].startswith("bert.")],
        #                      lr=config["hyperparameters"]["learning_rate"])
        #lossfunction = torch.nn.CrossEntropyLoss()

        weights = SelfAtt_BertTokenizing_Framework.get_class_weights(train_data.examples, "stance_label", 4, min_fraction=1)

        logging.info("class weights")
        logging.info(f"{str(weights.numpy().tolist())}")
        lossfunction = torch.nn.CrossEntropyLoss(weight=weights.to(device))
        start_time = time.time()
        try:
            best_val_loss = math.inf
            best_val_acc = 0
            best_val_F1 = 0

            self.predict("answer_BERTF1_textonly.json", model, dev_iter)
            for epoch in range(config["hyperparameters"]["epochs"]):
                self.epoch = epoch
                #self.run_epoch(model, lossfunction, optimizer, train_iter, config)
                log_results = epoch > 5
                train_loss, train_acc, _, train_F1 = self.validate(model, lossfunction, train_iter, config, log_results=False)
                validation_loss, validation_acc, val_acc_per_level, val_F1 = self.validate(model, lossfunction, dev_iter,
                                                                                   config, log_results=log_results)
                sorted_val_acc_pl = sorted(val_acc_per_level.items(), key=lambda x: int(x[0]))
                if validation_loss < best_val_loss:
                    best_val_loss = validation_loss
                if validation_acc > best_val_acc:
                    best_val_acc = validation_acc
                if val_F1 > best_val_F1:
                    best_val_F1 = val_F1

                logging.info(
                    f"Epoch {epoch}, Training loss|acc|F1: {train_loss:.6f}|{train_acc:.6f}|{train_F1:.6f}")
                logging.info(
                    f"Epoch {epoch}, Validation loss|acc|F1: {validation_loss:.6f}|{validation_acc:.6f}|{val_F1:.6f} - "
                    f"(Best {best_val_loss:.4f}|{best_val_acc:4f}|{best_val_F1})")

                logging.debug(
                    f"Epoch {epoch}, Validation loss|acc|F1: {validation_loss:.6f}|{validation_acc:.6f}|{val_F1:.6f} - "
                    f"(Best {best_val_loss:.4f}|{best_val_acc:4f}|{best_val_F1})")

                logging.debug("\n".join([f"{k} - {v:.2f}" for k, v in sorted_val_acc_pl]))
                if val_F1 > self.save_treshold:
                    # Map to CPU before saving, because this requires additional memory /for some reason/
                    model.to(torch.device("cpu"))
                    torch.save(model,
                               f"saved/checkpoint_{str(self.__class__)}_F1_{val_F1:.5f}_{get_timestamp()}.pt")
                    model.to(device)
        except KeyboardInterrupt:
            logging.info('-' * 120)
            logging.info('Exit from training early.')
        finally:
            logging.info(f'Finished after {(time.time() - start_time) / 60} minutes.')

    def validate(self, model: torch.nn.Module, lossfunction: _Loss, dev_iter: Iterator, config: dict, verbose=False,
                 log_results=True):
        train_flag = model.training
        model.eval()

        total_batches = len(dev_iter.data()) // dev_iter.batch_size
        if verbose:
            pbar = tqdm(total=total_batches)
        if log_results:
            csvf, writer = self.init_result_logging()
        examples_so_far = 0
        dev_loss = 0
        total_correct = 0
        total_correct_per_level = Counter()
        total_per_level = defaultdict(lambda: 0)
        total_labels = []
        total_preds = []
        for i, batch in enumerate(dev_iter):
            pred_logits = model(batch)

            loss = lossfunction(pred_logits, batch.stance_label)

            branch_levels = [id.split(".", 1)[-1] for id in batch.branch_id]
            for branch_depth in branch_levels: total_per_level[branch_depth] += 1
            correct, correct_per_level = self.calculate_correct(pred_logits, batch.stance_label, levels=branch_levels)
            total_correct += correct
            total_correct_per_level += correct_per_level

            examples_so_far += len(batch.stance_label)
            dev_loss += loss.item()
            if verbose:
                pbar.set_description(
                    f"dev loss: {dev_loss / (i + 1):.4f}, dev acc: {total_correct / examples_so_far:.4f}")
                pbar.update(1)

            maxpreds, argmaxpreds = torch.max(F.softmax(pred_logits, -1), dim=1)
            total_preds += list(argmaxpreds.cpu().numpy())
            total_labels += list(batch.stance_label.cpu().numpy())
            if log_results:
                text_s = [' '.join(self.tokenizer.convert_ids_to_tokens(batch.text[i].cpu().numpy())) for i in
                          range(batch.text.shape[0])]
                pred_s = list(argmaxpreds.cpu().numpy())
                target_s = list(batch.stance_label.cpu().numpy())
                correct_s = list((argmaxpreds == batch.stance_label).cpu().numpy())
                prob_s = [f"{x:.2f}" for x in
                          list(maxpreds.cpu().detach().numpy())]

                assert len(text_s) == len(pred_s) == len(correct_s) == len(
                    target_s) == len(prob_s)
                for i in range(len(text_s)):
                    writer.writerow([correct_s[i],
                                     batch.id[i],
                                     batch.tweet_id[i],
                                     branch_levels[i],
                                     map_stance_label_to_s[target_s[i]],
                                     map_stance_label_to_s[pred_s[i]],
                                     prob_s[i],
                                     batch.raw_text[i],
                                     text_s[i]])

        loss, acc = dev_loss / total_batches, total_correct / examples_so_far
        total_acc_per_level = {depth: total_correct_per_level.get(depth, 0) / total for depth, total in
                               total_per_level.items()}
        F1 = metrics.f1_score(total_labels, total_preds, average="macro")
        if log_results:
            self.finalize_results_logging(csvf, loss, F1)
        if train_flag:
            model.train()
        return loss, acc, total_acc_per_level, F1

    def finalize_results_logging(self, csvf, loss, f1):
        csvf.close()
        os.rename(self.TMP_FNAME, f"introspection/introspection"
        f"_{str(self.__class__)}_A{f1:.6f}_L{loss:.6f}_{socket.gethostname()}.tsv", )

    RESULT_HEADER = ["Correct",
                     "data_id",
                     "tweet_id",
                     "branch_level",
                     "Ground truth",
                     "Prediction",
                     "Confidence",
                     "Text",
                     "Processed_Text"]

    def init_result_logging(self):
        self.TMP_FNAME = f"introspection/TMP_introspection_{str(self.__class__)}_{socket.gethostname()}.tsv"
        csvf = open(self.TMP_FNAME, mode="w")
        writer = csv.writer(csvf, delimiter='\t')
        writer.writerow(self.__class__.RESULT_HEADER)
        return csvf, writer
