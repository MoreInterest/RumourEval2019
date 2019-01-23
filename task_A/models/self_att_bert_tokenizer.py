import logging

import torch
from pytorch_pretrained_bert import BertModel
from pytorch_pretrained_bert.modeling import PreTrainedBertModel
from torch import nn

from encoders import SelfAttentiveEncoder


def dump_batch_contents(batch):
    logging.debug("#" * 30)
    logging.debug("Dumping batch contents")
    for i in range(batch.text.shape[0]):
        logging.debug(f"L:{len(batch.text[i])} T: {batch.raw_text[i]}")


class SelfAttWithBertTokenizing(PreTrainedBertModel):
    """
        `input_ids`: a torch.LongTensor of shape [batch_siz, sequence_length]
        `token_type_ids`: an optional torch.LongTensor of shape [batch_size, sequence_length]
            with the token types indices selected in [0, 1]. Type 0 corresponds to a `sentence A`
            and type 1 corresponds to a `sentence B` token (see BERT paper for more details).

        `attention_mask`: an optional torch.LongTensor of shape [batch_size, sequence_length] with indices
            selected in [0, 1]. It's a mask to be used if the input sequence length is smaller than the max
            input sequence length in the current batch. It's the mask that we typically use for attention when
            a batch has varying length sentences.
    """

    def __init__(self, config, classes=4):
        super(SelfAttWithBertTokenizing, self).__init__(config)
        self.bert = BertModel(config)
        # hack token type embeddings
        # self.bert.embeddings.token_type_embeddings = torch.nn.Embedding(3,768)
        self.dropout = nn.Dropout(config.hidden_dropout_prob)

        # self.bertout_layer = nn.Linear(config.hidden_size, config.hidden_size)
        # self.hidden_layer = nn.Linear(config.hidden_size, config.hidden_size+20)
        # self.last_layer = nn.Linear(config.hidden_size+50, classes)
        self.apply(self.init_bert_weights)

    def extra_init(self, config, tokenizer, classes=4):
        self.tokenizer = tokenizer
        self.encoder = SelfAttentiveEncoder(config["hyperparameters"])
        self.dropout_rate = config["hyperparameters"]["dropout_rate"]
        # self.hidden = torch.nn.Linear(self.encoder.get_output_dim(), 314)
        self.final_layer = torch.nn.Linear(self.encoder.get_output_dim(), classes)

        self.apply(self.init_bert_weights)

    def forward(self, batch):
        # Since attention_mask is 1.0 for positions we want to attend and 0.0 for
        # masked positions, this operation will create a tensor which is 0.0 for
        # positions we want to attend and -10000.0 for masked positions.
        # Since we are adding it to the raw scores before the softmax, this is
        # effectively the same as removing these entirely.

        embedding_output = self.bert.embeddings(batch.text, batch.type_mask)
        h, attention = self.encoder(batch.text, embedding_output, padtoken = self.tokenizer.vocab["[PAD]"])
        # for fc in self.fclayers:
        #     h = F.dropout(F.relu(fc(h)), self.dropout_rate)

        r = h.view(h.shape[0], -1)
        # r = F.dropout(F.relu(self.hidden(r)), self.dropout_rate)
        return self.final_layer(r), attention
