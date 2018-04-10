import numpy as np
import tensorflow as tf

from deeppavlov.core.layers.tf_layers import embedding_layer, character_embedding_network, variational_dropout
from deeppavlov.core.layers.tf_layers import cudnn_bi_lstm, cudnn_bi_gru, bi_rnn, stacked_cnn
from deeppavlov.core.models.tf_model import TFModel
from deeppavlov.core.common.utils import check_gpu_existance

INITIALIZER = tf.orthogonal_initializer

class NerNetwork(TFModel):
    GRAPH_PARAMS = ["n_filters",  # TODO: add check
                    "filter_width",
                    "token_embeddings_dim",
                    "char_embeddings_dim",
                    "use_char_embeddings",
                    "use_batch_norm",
                    "use_crf",
                    "net_type",
                    "char_filter_width",
                    "cell_type"]

    def __init__(self,
                 n_tags,  # Features dimensions
                 char_emb_dim=None,
                 capitalization_dim=None,
                 pos_features_dim=None,
                 net_type='rnn',  # Net architecture
                 cell_type='lstm',
                 use_cudnn_rnn=False,
                 two_dense_on_top=False,
                 n_hidden_list=(128,),
                 cnn_filter_width=7,
                 use_crf=False,
                 token_emb_mat=None,
                 char_emb_mat=None,
                 use_batch_norm=False,  # Regularization
                 embeddings_dropout=False,
                 top_dropout=False,
                 intra_layer_dropout=False,
                 l2_reg=0.0,
                 clip_grad_norm=5.0,
                 gpu=None):
        self._build_training_placeholders()
        self._xs_placeholders = []
        self._y_ph = tf.placeholder(tf.int32, [None, None], name='y_ph')
        self._input_features = []

        # ================ Building input features =================

        # Token embeddings
        self._build_word_embeddings(token_emb_mat, embeddings_dropout)

        # Masks for different lengths utterances
        mask_ph = self._build_mask()

        # Char embeddings using highway CNN with max pooling
        if char_emb_dim is not None:
            self._build_char_embeddings(char_emb_mat, embeddings_dropout)

        # Capitalization features
        if capitalization_dim is not None:
            self._build_capitalization(capitalization_dim)

        # Part of speech features
        if pos_features_dim is not None:
            self._build_pos(pos_features_dim)

        features = tf.concat(self._input_features)

        # ================== Building the network ==================

        if net_type == 'rnn':
            if use_cudnn_rnn:
                if l2_reg > 0:
                    raise Warning('cuDNN RNN are not l2 regularizable')
                units = self._build_cudnn_rnn(features, n_hidden_list, cell_type, intra_layer_dropout)
            else:
                units = self._build_rnn(features, n_hidden_list, cell_type, intra_layer_dropout)
        elif net_type == 'cnn':
            units = self._build_cnn(features, n_hidden_list, cnn_filter_width, use_batch_norm)
        logits = self._build_top(units, n_tags, n_hidden_list[-1], top_dropout, two_dense_on_top)

        self.train_op, self.loss, predict_method = self._build_train_predict(logits, mask_ph, n_tags, use_crf,
                                                                             clip_grad_norm, l2_reg)
        self.predict = predict_method

        # ================= Initialize the session =================

        sess_config = tf.ConfigProto(allow_soft_placement=True)
        sess_config.gpu_options.allow_growth = True
        if gpu is not None:
            sess_config.gpu_options.visible_device_list = str(gpu)

        self.sess = tf.Session(sess_config)

    def _build_training_placeholders(self):
        self.learning_rate_ph = tf.placeholder(dtype=tf.float32, shape=[], name='learning_rate')
        self._dropout_ph = tf.placeholder_with_default(1.0, shape=[], name='dropout')
        self.training_ph = tf.placeholder_with_default(False, shape=[], name='is_training')

    def _build_word_embeddings(self, token_emb_mat, embeddings_dropout):
        token_indices_ph = tf.placeholder(tf.int32, [None, None])
        emb = embedding_layer(token_indices_ph, token_emb_mat)
        if embeddings_dropout:
            emb = tf.layers.dropout(emb, self._dropout_ph, noise_shape=[tf.shape(emb)[0], 1, tf.shape(emb)[2]])
        self._xs_placeholders.append(token_indices_ph)
        self._input_features.append(emb)

    def _build_mask(self):
        mask_ph = tf.placeholder(tf.float32, [None, None], name='Mask_ph')
        self._xs_placeholders.append(mask_ph)
        return mask_ph

    def _build_char_embeddings(self, char_emb_mat, embeddings_dropout):
        character_indices_ph = tf.placeholder(tf.int32, [None, None, None], name='Char_ph')
        character_embedding_network()

    def _build_capitalization(self, capitalization_dim):
        capitalization_ph = tf.placeholder(tf.int32, [None, None, capitalization_dim], name='Capitalization_ph')
        self._xs_placeholders.append(capitalization_ph)
        self._input_features.append(capitalization_ph)

    def _build_pos(self, pos_features_dim):
        pos_ph = tf.placeholder(tf.int32, [None, None, pos_features_dim], name='POS_ph')
        self._xs_placeholders.append(pos_ph)
        self._input_features.append(pos_ph)

    def _build_cudnn_rnn(self, units, n_hidden_list, cell_type, intra_layer_dropout):
        if not check_gpu_existance():
            raise RuntimeError('Usage of cuDNN RNN layers require GPU along with cuDNN library')

        for n, n_hidden in enumerate(n_hidden_list):
            with tf.variable_scope(cell_type.upper() + '_' + str(n)):
                if cell_type.lower() == 'lstm':
                    units, _ = cudnn_bi_lstm(units, n_hidden)
                elif cell_type.lower() == 'gru':
                    units, _ = cudnn_bi_gru(units, n_hidden)
                else:
                    raise RuntimeError('Wrong cell type "{}"! Only "gru" and "lstm"!'.format(cell_type))
                units = tf.concat(units, -1)
                if intra_layer_dropout and n != len(n_hidden_list) - 1:
                    units = variational_dropout(units, self._dropout_ph)
            return units

    def _build_rnn(self, units, n_hidden_list, cell_type, intra_layer_dropout):
        for n, n_hidden in enumerate(n_hidden_list):
            units, _ = bi_rnn(units, n_hidden, cell_type=cell_type, name='Layer_' + str(n))
            units = tf.concat(units, -1)
            if intra_layer_dropout and n != len(n_hidden_list) - 1:
                units = variational_dropout(units, self._dropout_ph)
        return units

    def _build_cnn(self, units, n_hidden_list, cnn_filter_width, use_batch_norm):
        units = stacked_cnn(units, n_hidden_list, cnn_filter_width, use_batch_norm, self.training_ph)
        return units

    def _build_top(self, units, n_tags, n_hididden, top_dropout, two_dense_on_top):
        if top_dropout:
            units = variational_dropout(units, self._dropout_ph)
        if two_dense_on_top:
            units = tf.layers.dense(units, n_hididden, activation=tf.nn.relu,
                                    kernel_initializer=INITIALIZER(),
                                    kernel_regularizer=tf.nn.l2_loss)
        logits = tf.layers.dense(units, n_tags, activation=None,
                                 kernel_initializer=INITIALIZER(),
                                 kernel_regularizer=tf.nn.l2_loss)
        return logits

    def _build_train_predict(self, logits, mask, n_tags, use_crf, clip_grad_norm, l2_reg):
        if use_crf:
            sequence_lengths = tf.reduce_sum(mask, axis=1)
            log_likelihood, transition_params = tf.contrib.crf.crf_log_likelihood(logits, self._y_ph, sequence_lengths)
            loss_tensor = -log_likelihood
        else:
            ground_truth_labels = tf.one_hot(self._y_ph, n_tags)
            loss_tensor = tf.nn.softmax_cross_entropy_with_logits(labels=ground_truth_labels, logits=logits)
            loss_tensor = loss_tensor * mask

        loss = tf.reduce_mean(loss_tensor)

        # L2 regularization
        if l2_reg > 0:
            total_loss = loss + l2_reg * tf.reduce_mean(tf.get_collection(tf.GraphKeys.REGULARIZATION_LOSSES))
        else:
            total_loss = loss

        train_op = self.get_train_op(total_loss, self.learning_rate_ph, clip_norm=clip_grad_norm)

        return train_op, loss,

    def predict_no_crf(self, *args):
        feed_dict = {ph: val for ph, val in zip(self._xs_placeholders, args)}
        if self._use_crf:
            y_pred = []
            logits, trans_params, sequence_lengths = self._sess.run([self._logits,
                                                                     self._transition_params,
                                                                     self._sequence_lengths],
                                                                    feed_dict=feed_dict)
            # iterate over the sentences because no batching in viterbi_decode
            for logit, sequence_length in zip(logits, sequence_lengths):
                logit = logit[:int(sequence_length)]  # keep only the valid steps
                viterbi_seq, viterbi_score = tf.contrib.crf.viterbi_decode(logit, trans_params)
                y_pred += [viterbi_seq]
        else:
            y_pred = self._sess.run(self._y_pred, feed_dict=feed_dict)
        return y_pred

    def _fill_feed_dict(self, *args):

    def __call__(self, *args, **kwargs):
        pass

    def train_on_batch(self, x: list, y: list):
        pass

    def save(self, *args, **kwargs):
        pass

    def load(self, *args, **kwargs):
        pass