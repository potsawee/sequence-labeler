import collections
import tensorflow as tf
import re
import numpy
from tensorflow.python.framework import ops
from tensorflow.python.ops import math_ops

try:
    import cPickle as pickle
except:
    import pickle

# import pdb --- python debugger

class SequenceLabeler(object):
    def __init__(self, config):
        self.config = config

        self.UNK = "<unk>"
        self.CUNK = "<cunk>"

        self.word2id = None
        self.char2id = None
        self.label2id = None
        self.singletons = None

        # --------- Attention Mechanism Config --------- #
        if "attention_char_lstm" not in self.config:
            self.config["attention_char_lstm"] = False
        if "attention_word_lstm" not in self.config:
            self.config["attention_word_lstm"] = False
        # ---------------------------------------------- #

    def build_vocabs(self, data_train, data_dev, data_test, embedding_path=None):
        data_source = list(data_train)
        if self.config["vocab_include_devtest"]:
            if data_dev != None:
                data_source += data_dev
            if data_test != None:
                data_source += data_test

        char_counter = collections.Counter()
        for sentence in data_source:
            for word in sentence:
                char_counter.update(word[0])
        self.char2id = collections.OrderedDict([(self.CUNK, 0)])
        for char, count in char_counter.most_common():
            if char not in self.char2id:
                self.char2id[char] = len(self.char2id)

        word_counter = collections.Counter()
        for sentence in data_source:
            for word in sentence:
                w = word[0]
                if self.config["lowercase"] == True:
                    w = w.lower()
                if self.config["replace_digits"] == True:
                    w = re.sub(r'\d', '0', w)
                word_counter[w] += 1
        self.word2id = collections.OrderedDict([(self.UNK, 0)])
        for word, count in word_counter.most_common():
            if self.config["min_word_freq"] <= 0 or count >= self.config["min_word_freq"]:
                if word not in self.word2id:
                    self.word2id[word] = len(self.word2id)

        self.singletons = set([word for word in word_counter if word_counter[word] == 1])

        label_counter = collections.Counter()
        for sentence in data_train: #this one only based on training data
            for word in sentence:
                label_counter[word[-1]] += 1
        self.label2id = collections.OrderedDict()
        for label, count in label_counter.most_common():
            if label not in self.label2id:
                self.label2id[label] = len(self.label2id)

        if embedding_path != None and self.config["vocab_only_embedded"] == True:
            self.embedding_vocab = set([self.UNK])
            with open(embedding_path, 'r') as f:
                for line in f:
                    line_parts = line.strip().split()
                    if len(line_parts) <= 2:
                        continue
                    w = line_parts[0]
                    if self.config["lowercase"] == True:
                        w = w.lower()
                    if self.config["replace_digits"] == True:
                        w = re.sub(r'\d', '0', w)
                    self.embedding_vocab.add(w)
            word2id_revised = collections.OrderedDict()
            for word in self.word2id:
                if word in embedding_vocab and word not in word2id_revised:
                    word2id_revised[word] = len(word2id_revised)
            self.word2id = word2id_revised

        print("n_words: " + str(len(self.word2id)))
        print("n_chars: " + str(len(self.char2id)))
        print("n_labels: " + str(len(self.label2id)))
        print("n_singletons: " + str(len(self.singletons)))


    def construct_network(self):
        self.word_ids = tf.placeholder(tf.int32, [None, None], name="word_ids")
        self.char_ids = tf.placeholder(tf.int32, [None, None, None], name="char_ids")
        self.sentence_lengths = tf.placeholder(tf.int32, [None], name="sentence_lengths")
        self.word_lengths = tf.placeholder(tf.int32, [None, None], name="word_lengths")
        self.label_ids = tf.placeholder(tf.int32, [None, None], name="label_ids")
        self.learningrate = tf.placeholder(tf.float32, name="learningrate")
        self.is_training = tf.placeholder(tf.int32, name="is_training")

        self.loss = 0.0
        input_tensor = None
        input_vector_size = 0

        self.initializer = None
        if self.config["initializer"] == "normal":
            self.initializer = tf.random_normal_initializer(mean=0.0, stddev=0.1)
        elif self.config["initializer"] == "glorot":
            self.initializer = tf.glorot_uniform_initializer()
        elif self.config["initializer"] == "xavier":
            self.initializer = tf.glorot_normal_initializer()
        else:
            raise ValueError("Unknown initializer")

        self.word_embeddings = tf.get_variable("word_embeddings",
            shape=[len(self.word2id), self.config["word_embedding_size"]],
            initializer=(tf.zeros_initializer() if self.config["emb_initial_zero"] == True else self.initializer),
            trainable=(True if self.config["train_embeddings"] == True else False))
        input_tensor = tf.nn.embedding_lookup(self.word_embeddings, self.word_ids)
        input_vector_size = self.config["word_embedding_size"]

        if self.config["char_embedding_size"] > 0 and self.config["char_recurrent_size"] > 0:
            with tf.variable_scope("chars"), tf.control_dependencies([tf.assert_equal(tf.shape(self.char_ids)[2], tf.reduce_max(self.word_lengths), message="Char dimensions don't match")]):
                self.char_embeddings = tf.get_variable("char_embeddings",
                    shape=[len(self.char2id), self.config["char_embedding_size"]],
                    initializer=self.initializer,
                    trainable=True)

                char_input_tensor = tf.nn.embedding_lookup(self.char_embeddings, self.char_ids)

                s = tf.shape(char_input_tensor)
                char_input_tensor = tf.reshape(char_input_tensor, shape=[s[0]*s[1], s[2], self.config["char_embedding_size"]])
                _word_lengths = tf.reshape(self.word_lengths, shape=[s[0]*s[1]])

                char_lstm_cell_fw = tf.nn.rnn_cell.LSTMCell(self.config["char_recurrent_size"],
                    use_peepholes=self.config["lstm_use_peepholes"],
                    state_is_tuple=True,
                    initializer=self.initializer,
                    reuse=False)
                char_lstm_cell_bw = tf.nn.rnn_cell.LSTMCell(self.config["char_recurrent_size"],
                    use_peepholes=self.config["lstm_use_peepholes"],
                    state_is_tuple=True,
                    initializer=self.initializer,
                    reuse=False)

                if self.config["attention_char_lstm"] == False:
                    char_lstm_outputs = tf.nn.bidirectional_dynamic_rnn(char_lstm_cell_fw, char_lstm_cell_bw, char_input_tensor, sequence_length=_word_lengths, dtype=tf.float32, time_major=False)
                    _, ((_, char_output_fw), (_, char_output_bw)) = char_lstm_outputs
                    char_output_tensor = tf.concat([char_output_fw, char_output_bw], axis=-1)
                    char_output_tensor = tf.reshape(char_output_tensor, shape=[s[0], s[1], 2 * self.config["char_recurrent_size"]])
                    char_output_vector_size = 2 * self.config["char_recurrent_size"]

                else:
                    # -------- Attention Mechanism -------- #
                    # s[0] = batch_size
                    # s[1] = max_sentence_length
                    # s[2] = max_word_legnth
                    # s[3] = char_embedding_size

                    # ------------- Version 1 ------------- #
                    # (char_lstm_outputs_fw, char_lstm_outputs_bw), _ = \
                    #     tf.nn.bidirectional_dynamic_rnn(char_lstm_cell_fw, char_lstm_cell_bw,
                    #                                     char_input_tensor,
                    #                                     dtype=tf.float32, time_major=False)
                    #
                    # attention_fw_matrix = tf.get_variable(name="attention_fw_matrix", shape=[self.config["char_recurrent_size"],1],
                    #                                     initializer=self.initializer, trainable=True)
                    # char_lstm_outputs_fw = tf.reshape(char_lstm_outputs_fw,shape=[s[0]*s[1]*s[2],self.config["char_recurrent_size"]])
                    # attention_fw = tf.matmul(char_lstm_outputs_fw, attention_fw_matrix)
                    # attention_fw = tf.nn.softmax(tf.reshape(attention_fw, shape=[s[0]*s[1],s[2]]))
                    # attention_fw = tf.reshape(attention_fw, shape=[s[0]*s[1]*s[2],1])
                    # attention_fw_output = tf.reshape(tf.multiply(attention_fw, char_lstm_outputs_fw),
                    #                               shape=[s[0], s[1], s[2], self.config["char_recurrent_size"]])
                    # attention_fw_output = tf.reduce_sum(attention_fw_output, axis=2)
                    #
                    # attention_bw_matrix = tf.get_variable(name="attention_bw_matrix", shape=[self.config["char_recurrent_size"],1],
                    #                                     initializer=self.initializer, trainable=True)
                    # char_lstm_outputs_bw = tf.reshape(char_lstm_outputs_bw,shape=[s[0]*s[1]*s[2],self.config["char_recurrent_size"]])
                    # attention_bw = tf.matmul(char_lstm_outputs_bw, attention_bw_matrix)
                    # attention_bw = tf.nn.softmax(tf.reshape(attention_bw, shape=[s[0]*s[1],s[2]]))
                    # attention_bw = tf.reshape(attention_bw, shape=[s[0]*s[1]*s[2],1])
                    # attention_bw_output = tf.reshape(tf.multiply(attention_bw, char_lstm_outputs_bw),
                    #                               shape=[s[0], s[1], s[2], self.config["char_recurrent_size"]])
                    #
                    # attention_bw_output = tf.reduce_sum(attention_bw_output, axis=2)
                    #
                    # char_output_tensor = tf.concat([attention_fw_output,attention_bw_output], axis=-1)
                    # char_output_vector_size = 2 * self.config["char_recurrent_size"]

                    # ------------- Version 2 ------------- #
                    (char_lstm_outputs_fw, char_lstm_outputs_bw), _ = \
                        tf.nn.bidirectional_dynamic_rnn(char_lstm_cell_fw, char_lstm_cell_bw,
                                                        char_input_tensor,
                                                        sequence_length=_word_lengths,
                                                        dtype=tf.float32, time_major=False)

                    attention_fw_matrix = tf.get_variable(name="attention_fw_matrix",
                                                        shape=[self.config["char_recurrent_size"],self.config["char_embedding_size"]],
                                                        initializer=tf.initializers.identity(), trainable=True)
                    attention_fw = tf.tensordot(char_lstm_outputs_fw, attention_fw_matrix, axes=((-1),(0)))
                    attention_fw = tf.reduce_sum(tf.multiply(attention_fw, char_input_tensor), axis=-1)
                    attention_fw = tf.nn.softmax(attention_fw)
                    attention_fw_output = tf.multiply(tf.reshape(attention_fw, shape=[s[0]*s[1]*s[2],1]),
                                                      tf.reshape(char_lstm_outputs_fw,shape=[s[0]*s[1]*s[2],self.config["char_recurrent_size"]]))
                    attention_fw_output = tf.reshape(attention_fw_output, shape=[s[0], s[1], s[2], self.config["char_recurrent_size"]])
                    attention_fw_output = tf.reduce_sum(attention_fw_output, axis=2)

                    attention_bw_matrix = tf.get_variable(name="attention_bw_matrix",
                                                        shape=[self.config["char_recurrent_size"],self.config["char_embedding_size"]],
                                                        initializer=tf.initializers.identity(), trainable=True)
                    attention_bw = tf.tensordot(char_lstm_outputs_bw, attention_bw_matrix, axes=((-1),(0)))
                    attention_bw = tf.reduce_sum(tf.multiply(attention_bw, char_input_tensor), axis=-1)
                    attention_bw = tf.nn.softmax(attention_bw)
                    attention_bw_output = tf.multiply(tf.reshape(attention_bw, shape=[s[0]*s[1]*s[2],1]),
                                                      tf.reshape(char_lstm_outputs_bw,shape=[s[0]*s[1]*s[2],self.config["char_recurrent_size"]]))
                    attention_bw_output = tf.reshape(attention_bw_output, shape=[s[0], s[1], s[2], self.config["char_recurrent_size"]])
                    attention_bw_output = tf.reduce_sum(attention_bw_output, axis=2)

                    char_output_tensor = tf.concat([attention_fw_output,attention_bw_output], axis=-1)
                    char_output_vector_size = 2 * self.config["char_recurrent_size"]
                    # ------------------------------------- #

                if self.config["lmcost_char_gamma"] > 0.0:
                    self.loss += self.config["lmcost_char_gamma"] * self.construct_lmcost(char_output_tensor, char_output_tensor, self.sentence_lengths, self.word_ids, "separate", "lmcost_char_separate")
                if self.config["lmcost_joint_char_gamma"] > 0.0:
                    self.loss += self.config["lmcost_joint_char_gamma"] * self.construct_lmcost(char_output_tensor, char_output_tensor, self.sentence_lengths, self.word_ids, "joint", "lmcost_char_joint")

                if self.config["char_hidden_layer_size"] > 0:
                    char_hidden_layer_size = self.config["word_embedding_size"] if self.config["char_integration_method"] == "attention" else self.config["char_hidden_layer_size"]
                    char_output_tensor = tf.layers.dense(char_output_tensor, char_hidden_layer_size, activation=tf.tanh, kernel_initializer=self.initializer)
                    char_output_vector_size = char_hidden_layer_size

                if self.config["char_integration_method"] == "concat":
                    input_tensor = tf.concat([input_tensor, char_output_tensor], axis=-1)
                    input_vector_size += char_output_vector_size
                elif self.config["char_integration_method"] == "attention":
                    assert(char_output_vector_size == self.config["word_embedding_size"]), "This method requires the char representation to have the same size as word embeddings"
                    static_input_tensor = tf.stop_gradient(input_tensor)
                    is_unk = tf.equal(self.word_ids, self.word2id[self.UNK])
                    char_output_tensor_normalised = tf.nn.l2_normalize(char_output_tensor, 2)
                    static_input_tensor_normalised = tf.nn.l2_normalize(static_input_tensor, 2)
                    cosine_cost = 1.0 - tf.reduce_sum(tf.multiply(char_output_tensor_normalised, static_input_tensor_normalised), axis=2)
                    is_padding = tf.logical_not(tf.sequence_mask(self.sentence_lengths, maxlen=tf.shape(self.word_ids)[1]))
                    cosine_cost_unk = tf.where(tf.logical_or(is_unk, is_padding), x=tf.zeros_like(cosine_cost), y=cosine_cost)
                    self.loss += self.config["char_attention_cosine_cost"] * tf.reduce_sum(cosine_cost_unk)
                    attention_evidence_tensor = tf.concat([input_tensor, char_output_tensor], axis=2)
                    attention_output = tf.layers.dense(attention_evidence_tensor, self.config["word_embedding_size"], activation=tf.tanh, kernel_initializer=self.initializer)
                    attention_output = tf.layers.dense(attention_output, self.config["word_embedding_size"], activation=tf.sigmoid, kernel_initializer=self.initializer)
                    input_tensor = tf.multiply(input_tensor, attention_output) + tf.multiply(char_output_tensor, (1.0 - attention_output))
                elif self.config["char_integration_method"] == "none":
                    input_tensor = input_tensor
                else:
                    raise ValueError("Unknown char integration method")

        dropout_input = self.config["dropout_input"] * tf.cast(self.is_training, tf.float32) + (1.0 - tf.cast(self.is_training, tf.float32))
        input_tensor =  tf.nn.dropout(input_tensor, dropout_input, name="dropout_word")

        word_lstm_cell_fw = tf.nn.rnn_cell.LSTMCell(self.config["word_recurrent_size"],
            use_peepholes=self.config["lstm_use_peepholes"],
            state_is_tuple=True,
            initializer=self.initializer,
            reuse=False)
        word_lstm_cell_bw = tf.nn.rnn_cell.LSTMCell(self.config["word_recurrent_size"],
            use_peepholes=self.config["lstm_use_peepholes"],
            state_is_tuple=True,
            initializer=self.initializer,
            reuse=False)

        if self.config["attention_word_lstm"] == False:
            with tf.control_dependencies([tf.assert_equal(tf.shape(self.word_ids)[1], tf.reduce_max(self.sentence_lengths), message="Sentence dimensions don't match")]):
                (lstm_outputs_fw, lstm_outputs_bw), _ = tf.nn.bidirectional_dynamic_rnn(word_lstm_cell_fw, word_lstm_cell_bw, input_tensor, sequence_length=self.sentence_lengths, dtype=tf.float32, time_major=False)

        else:
            # ---------------- Word-Level Attention Mechanism ---------------- #
            # @potsawee - 26 July 2018

            # previous h_t is obtained from concatinating h_fw_t with h_bw_t
            # now we use c_t to represent word_t by c_t = sum alpha_t * h_t

            # input_tensor shape = [batch_size, max_sentence_length, word_vector_representation_size]
            # s_ip = tf.shape(input_tensor)

            # let
            # B = batch_size
            # M = max_sentecce_length
            # N = word_recurrent_size
            # lstm_outputs_fw           [B,M,N]
            # lstm_attention_fw_weight_c  [2N,2N]
            # lstm_attention_fw_weight_a  [N,N] => For the 'general' content score function

            # Global Attention Model using the 'general' content score function
            # https://arxiv.org/pdf/1508.04025.pdf
            # Effective Approaches to Attention-based Neural Machine Translation by Luong (2015)

            # Multihead Attention
            # https://arxiv.org/pdf/1706.03762.pdf
            # Attention Is All You Need (2017)

            (lstm_outputs_fw, lstm_outputs_bw), _ = \
                tf.nn.bidirectional_dynamic_rnn(word_lstm_cell_fw, word_lstm_cell_bw,
                                                input_tensor,
                                                sequence_length=self.sentence_lengths,
                                                dtype=tf.float32, time_major=False)

            if "multihead" not in self.config:
                multihead = 1
            else:
                multihead = self.config["multihead"]

            assert(self.config["word_recurrent_size"] % multihead == 0, "Multihead size invalid")
            step_size = self.config["word_recurrent_size"]/multihead

            # Forward
            # lstm_attention_fw_weight_c = tf.get_variable(name="lstm_attention_fw_weight_c",
            #                                            shape=[2*self.config["word_recurrent_size"], 2*self.config["word_recurrent_size"]],
            #                                            initializer=self.initializer, trainable=True)
            context_fws = []
            for i in range(multihead):
                lstm_attention_fw_weight_a = tf.get_variable(name="lstm_attention_fw_weight_a" + str(i),
                                                           shape=[step_size, step_size],
                                                           initializer=tf.initializers.identity(), trainable=True)

                lstm_attention_fw = tf.tensordot(lstm_outputs_fw[:,:,i*step_size:(i+1)*step_size], lstm_attention_fw_weight_a, axes=((-1),(0)))  # [B,M,N]
                lstm_attention_fw = tf.matmul(lstm_attention_fw, tf.transpose(lstm_outputs_fw[:,:,i*step_size:(i+1)*step_size], perm=[0,2,1])) # matmul([B,M,N], B,N,M]) => [B,M,M]
                lstm_attention_fw = tf.nn.softmax(lstm_attention_fw) # [B,M,M]
                context_fws.append(tf.matmul(lstm_attention_fw, lstm_outputs_fw[:,:,i*step_size:(i+1)*step_size])) # [B,M,N]
            context_fw = tf.concat(context_fws, axis=-1)

            lstm_attention_fw_output = tf.concat([lstm_outputs_fw, context_fw], axis=-1) # [B,M,2N]
            # lstm_attention_fw_output = tf.tensordot(lstm_attention_fw_output, lstm_attention_fw_weight_c, axes=((-1),(0))) # [B,M,2N]
            # lstm_attention_fw_output = tf.tanh(lstm_attention_fw_output) # [B,M,2N]

            # Backward
            # lstm_attention_bw_weight_c = tf.get_variable(name="lstm_attention_bw_weight_c",
            #                                            shape=[2*self.config["word_recurrent_size"], 2*self.config["word_recurrent_size"]],
            #                                            initializer=self.initializer, trainable=True)
            context_bws = []
            for i in range(multihead):
                lstm_attention_bw_weight_a = tf.get_variable(name="lstm_attention_bw_weight_a" + str(i),
                                                           shape=[step_size, step_size],
                                                           initializer=tf.initializers.identity(), trainable=True)

                lstm_attention_bw = tf.tensordot(lstm_outputs_bw[:,:,i*step_size:(i+1)*step_size], lstm_attention_bw_weight_a, axes=((-1),(0)))  # [B,M,N]
                lstm_attention_bw = tf.matmul(lstm_attention_bw, tf.transpose(lstm_outputs_bw[:,:,i*step_size:(i+1)*step_size], perm=[0,2,1])) # matmul([B,M,N], B,N,M]) => [B,M,M]
                lstm_attention_bw = tf.nn.softmax(lstm_attention_bw) # [B,M,M]
                context_bws.append(tf.matmul(lstm_attention_bw, lstm_outputs_bw[:,:,i*step_size:(i+1)*step_size])) # [B,M,N]
            context_bw = tf.concat(context_bws, axis=-1)

            lstm_attention_bw_output = tf.concat([lstm_outputs_bw, context_bw], axis=-1) # [B,M,2N]
            # lstm_attention_bw_output = tf.tensordot(lstm_attention_bw_output, lstm_attention_bw_weight_c, axes=((-1),(0))) # [B,M,2N]
            # lstm_attention_bw_output = tf.tanh(lstm_attention_bw_output) # [B,M,2N]

            # To be consistent with the next part of the code
            lstm_outputs_fw = lstm_attention_fw_output
            lstm_outputs_bw = lstm_attention_bw_output
            # ---------------------------------------------------------------- #


        dropout_word_lstm = self.config["dropout_word_lstm"] * tf.cast(self.is_training, tf.float32) + (1.0 - tf.cast(self.is_training, tf.float32))
        lstm_outputs_fw =  tf.nn.dropout(lstm_outputs_fw, dropout_word_lstm)
        lstm_outputs_bw =  tf.nn.dropout(lstm_outputs_bw, dropout_word_lstm)

        if self.config["lmcost_lstm_gamma"] > 0.0:
            self.loss += self.config["lmcost_lstm_gamma"] * self.construct_lmcost(lstm_outputs_fw, lstm_outputs_bw, self.sentence_lengths, self.word_ids, "separate", "lmcost_lstm_separate")
        if self.config["lmcost_joint_lstm_gamma"] > 0.0:
            self.loss += self.config["lmcost_joint_lstm_gamma"] * self.construct_lmcost(lstm_outputs_fw, lstm_outputs_bw, self.sentence_lengths, self.word_ids, "joint", "lmcost_lstm_joint")

        processed_tensor = tf.concat([lstm_outputs_fw, lstm_outputs_bw], 2)
        # processed_tensor_size = self.config["word_recurrent_size"] * 2
        processed_tensor_size = processed_tensor.get_shape()[-1]

        if self.config["hidden_layer_size"] > 0:
            processed_tensor = tf.layers.dense(processed_tensor, self.config["hidden_layer_size"], activation=tf.tanh, kernel_initializer=self.initializer)
            processed_tensor_size = self.config["hidden_layer_size"]

        self.scores = tf.layers.dense(processed_tensor, len(self.label2id), activation=None, kernel_initializer=self.initializer, name="output_ff")

        if self.config["crf_on_top"] == True:
            crf_num_tags = self.scores.get_shape()[2].value
            self.crf_transition_params = tf.get_variable("output_crf_transitions", [crf_num_tags, crf_num_tags], initializer=self.initializer)
            log_likelihood, self.crf_transition_params = tf.contrib.crf.crf_log_likelihood(self.scores, self.label_ids, self.sentence_lengths, transition_params=self.crf_transition_params)
            self.loss += self.config["main_cost"] * tf.reduce_sum(-log_likelihood)
        else:
            self.probabilities = tf.nn.softmax(self.scores)
            self.predictions = tf.argmax(self.probabilities, 2)
            loss_ = tf.nn.sparse_softmax_cross_entropy_with_logits(logits=self.scores, labels=self.label_ids)
            mask = tf.sequence_mask(self.sentence_lengths, maxlen=tf.shape(self.word_ids)[1])
            loss_ = tf.boolean_mask(loss_, mask)
            self.loss += self.config["main_cost"] * tf.reduce_sum(loss_)

        self.train_op = self.construct_optimizer(self.config["opt_strategy"], self.loss, self.learningrate, self.config["clip"])


    def construct_lmcost(self, input_tensor_fw, input_tensor_bw, sentence_lengths, target_ids, lmcost_type, name):
        with tf.variable_scope(name):
            lmcost_max_vocab_size = min(len(self.word2id), self.config["lmcost_max_vocab_size"])
            target_ids = tf.where(tf.greater_equal(target_ids, lmcost_max_vocab_size-1), x=(lmcost_max_vocab_size-1)+tf.zeros_like(target_ids), y=target_ids)
            cost = 0.0
            if lmcost_type == "separate":
                lmcost_fw_mask = tf.sequence_mask(sentence_lengths, maxlen=tf.shape(target_ids)[1])[:,1:]
                lmcost_bw_mask = tf.sequence_mask(sentence_lengths, maxlen=tf.shape(target_ids)[1])[:,:-1]
                lmcost_fw = self._construct_lmcost(input_tensor_fw[:,:-1,:], lmcost_max_vocab_size, lmcost_fw_mask, target_ids[:,1:], name=name+"_fw")
                lmcost_bw = self._construct_lmcost(input_tensor_bw[:,1:,:], lmcost_max_vocab_size, lmcost_bw_mask, target_ids[:,:-1], name=name+"_bw")
                cost += lmcost_fw + lmcost_bw
            elif lmcost_type == "joint":
                joint_input_tensor = tf.concat([input_tensor_fw[:,:-2,:], input_tensor_bw[:,2:,:]], axis=-1)
                lmcost_mask = tf.sequence_mask(sentence_lengths, maxlen=tf.shape(target_ids)[1])[:,1:-1]
                cost += self._construct_lmcost(joint_input_tensor, lmcost_max_vocab_size, lmcost_mask, target_ids[:,1:-1], name=name+"_joint")
            else:
                raise ValueError("Unknown lmcost_type: " + str(lmcost_type))
            return cost


    def _construct_lmcost(self, input_tensor, lmcost_max_vocab_size, lmcost_mask, target_ids, name):
        with tf.variable_scope(name):
            lmcost_hidden_layer = tf.layers.dense(input_tensor, self.config["lmcost_hidden_layer_size"], activation=tf.tanh, kernel_initializer=self.initializer)
            lmcost_output = tf.layers.dense(lmcost_hidden_layer, lmcost_max_vocab_size, activation=None, kernel_initializer=self.initializer)
            lmcost_loss = tf.nn.sparse_softmax_cross_entropy_with_logits(logits=lmcost_output, labels=target_ids)
            lmcost_loss = tf.where(lmcost_mask, lmcost_loss, tf.zeros_like(lmcost_loss))
            return tf.reduce_sum(lmcost_loss)


    def construct_optimizer(self, opt_strategy, loss, learningrate, clip):
        optimizer = None
        if opt_strategy == "adadelta":
            optimizer = tf.train.AdadeltaOptimizer(learning_rate=learningrate)
        elif opt_strategy == "adam":
            optimizer = tf.train.AdamOptimizer(learning_rate=learningrate)
        elif opt_strategy == "sgd":
            optimizer = tf.train.GradientDescentOptimizer(learning_rate=learningrate)
        else:
            raise ValueError("Unknown optimisation strategy: " + str(opt_strategy))

        if clip > 0.0:
            grads, vs     = zip(*optimizer.compute_gradients(loss))
            grads, gnorm  = tf.clip_by_global_norm(grads, clip)
            train_op = optimizer.apply_gradients(zip(grads, vs))
        else:
            train_op = optimizer.minimize(loss)
        return train_op


    def preload_word_embeddings(self, embedding_path):
        loaded_embeddings = set()
        embedding_matrix = self.session.run(self.word_embeddings)
        with open(embedding_path, 'r') as f:
            for line in f:
                line_parts = line.strip().split()
                if len(line_parts) <= 2:
                    continue
                w = line_parts[0]
                if self.config["lowercase"] == True:
                    w = w.lower()
                if self.config["replace_digits"] == True:
                    w = re.sub(r'\d', '0', w)
                if w in self.word2id and w not in loaded_embeddings:
                    word_id = self.word2id[w]
                    embedding = numpy.array(line_parts[1:])
                    embedding_matrix[word_id] = embedding
                    loaded_embeddings.add(w)
        self.session.run(self.word_embeddings.assign(embedding_matrix))
        print("n_preloaded_embeddings: " + str(len(loaded_embeddings)))


    def translate2id(self, token, token2id, unk_token, lowercase=False, replace_digits=False, singletons=None, singletons_prob=0.0):
        if lowercase == True:
            token = token.lower()
        if replace_digits == True:
            token = re.sub(r'\d', '0', token)

        token_id = None
        if singletons != None and token in singletons and token in token2id and unk_token != None and numpy.random.uniform() < singletons_prob:
            token_id = token2id[unk_token]
        elif token in token2id:
            token_id = token2id[token]
        elif unk_token != None:
            token_id = token2id[unk_token]
        else:
            raise ValueError("Unable to handle value, no UNK token: " + str(token))
        return token_id


    def create_input_dictionary_for_batch(self, batch, is_training, learningrate):
        sentence_lengths = numpy.array([len(sentence) for sentence in batch])
        max_sentence_length = sentence_lengths.max()
        max_word_length = numpy.array([numpy.array([len(word[0]) for word in sentence]).max() for sentence in batch]).max()
        if self.config["allowed_word_length"] > 0 and self.config["allowed_word_length"] < max_word_length:
            max_word_length = min(max_word_length, self.config["allowed_word_length"])

        word_ids = numpy.zeros((len(batch), max_sentence_length), dtype=numpy.int32)
        char_ids = numpy.zeros((len(batch), max_sentence_length, max_word_length), dtype=numpy.int32)
        word_lengths = numpy.zeros((len(batch), max_sentence_length), dtype=numpy.int32)
        label_ids = numpy.zeros((len(batch), max_sentence_length), dtype=numpy.int32)

        singletons = self.singletons if is_training == True else None
        singletons_prob = self.config["singletons_prob"] if is_training == True else 0.0
        for i in range(len(batch)):
            for j in range(len(batch[i])):
                word_ids[i][j] = self.translate2id(batch[i][j][0], self.word2id, self.UNK, lowercase=self.config["lowercase"], replace_digits=self.config["replace_digits"], singletons=singletons, singletons_prob=singletons_prob)
                label_ids[i][j] = self.translate2id(batch[i][j][-1], self.label2id, None)
                word_lengths[i][j] = len(batch[i][j][0])
                for k in range(min(len(batch[i][j][0]), max_word_length)):
                    char_ids[i][j][k] = self.translate2id(batch[i][j][0][k], self.char2id, self.CUNK)
        input_dictionary = {self.word_ids: word_ids, self.char_ids: char_ids, self.sentence_lengths: sentence_lengths, self.word_lengths: word_lengths, self.label_ids: label_ids, self.learningrate: learningrate, self.is_training: is_training}
        return input_dictionary


    def viterbi_decode(self, score, transition_params):
        trellis = numpy.zeros_like(score)
        backpointers = numpy.zeros_like(score, dtype=numpy.int32)
        trellis[0] = score[0]

        for t in range(1, score.shape[0]):
            v = numpy.expand_dims(trellis[t - 1], 1) + transition_params
            trellis[t] = score[t] + numpy.max(v, 0)
            backpointers[t] = numpy.argmax(v, 0)

        viterbi = [numpy.argmax(trellis[-1])]
        for bp in reversed(backpointers[1:]):
            viterbi.append(bp[viterbi[-1]])
        viterbi.reverse()

        viterbi_score = numpy.max(trellis[-1])
        return viterbi, viterbi_score, trellis


    def process_batch(self, batch, is_training, learningrate):
        feed_dict = self.create_input_dictionary_for_batch(batch, is_training, learningrate)

        if self.config["crf_on_top"] == True:
            cost, scores = self.session.run([self.loss, self.scores] + ([self.train_op] if is_training == True else []), feed_dict=feed_dict)[:2]
            predicted_labels = []
            predicted_probs = []
            for i in range(len(batch)):
                sentence_length = len(batch[i])
                viterbi_seq, viterbi_score, viterbi_trellis = self.viterbi_decode(scores[i], self.session.run(self.crf_transition_params))
                predicted_labels.append(viterbi_seq[:sentence_length])
                predicted_probs.append(viterbi_trellis[:sentence_length])
        else:
            cost, predicted_labels_, predicted_probs_ = self.session.run([self.loss, self.predictions, self.probabilities] + ([self.train_op] if is_training == True else []), feed_dict=feed_dict)[:3]
            predicted_labels = []
            predicted_probs = []
            for i in range(len(batch)):
                sentence_length = len(batch[i])
                predicted_labels.append(predicted_labels_[i][:sentence_length])
                predicted_probs.append(predicted_probs_[i][:sentence_length])

        return cost, predicted_labels, predicted_probs


    def initialize_session(self):
        tf.set_random_seed(self.config["random_seed"])
        session_config = tf.ConfigProto()
        session_config = tf.ConfigProto(allow_soft_placement=True)
        session_config.gpu_options.allow_growth = self.config["tf_allow_growth"]
        session_config.gpu_options.per_process_gpu_memory_fraction = self.config["tf_per_process_gpu_memory_fraction"]
        self.session = tf.Session(config=session_config)
        self.session.run(tf.global_variables_initializer())
        self.saver = tf.train.Saver(max_to_keep=1)


    def get_parameter_count(self):
        total_parameters = 0
        for variable in tf.trainable_variables():
            shape = variable.get_shape()
            variable_parameters = 1
            for dim in shape:
                variable_parameters *= dim.value
            total_parameters += variable_parameters
        return total_parameters


    def get_parameter_count_without_word_embeddings(self):
        shape = self.word_embeddings.get_shape()
        variable_parameters = 1
        for dim in shape:
            variable_parameters *= dim.value
        return self.get_parameter_count() - variable_parameters


    def save(self, filename):
        dump = {}
        dump["config"] = self.config
        dump["UNK"] = self.UNK
        dump["CUNK"] = self.CUNK
        dump["word2id"] = self.word2id
        dump["char2id"] = self.char2id
        dump["label2id"] = self.label2id
        dump["singletons"] = self.singletons

        dump["params"] = {}
        for variable in tf.global_variables():
            assert(variable.name not in dump["params"]), "Error: variable with this name already exists" + str(variable.name)
            dump["params"][variable.name] = self.session.run(variable)
        with open(filename, 'wb') as f:
            pickle.dump(dump, f, protocol=pickle.HIGHEST_PROTOCOL)


    @staticmethod
    def load(filename):
        with open(filename, 'rb') as f:
            dump = pickle.load(f)

            # for safety, so we don't overwrite old models
            dump["config"]["save"] = None

            labeler = SequenceLabeler(dump["config"])
            labeler.UNK = dump["UNK"]
            labeler.CUNK = dump["CUNK"]
            labeler.word2id = dump["word2id"]
            labeler.char2id = dump["char2id"]
            labeler.label2id = dump["label2id"]
            labeler.singletons = dump["singletons"]

            labeler.construct_network()
            labeler.initialize_session()
            labeler.load_params(filename)

            return labeler


    def load_params(self, filename):
        with open(filename, 'rb') as f:
            dump = pickle.load(f)

            for variable in tf.global_variables():
                assert(variable.name in dump["params"]), "Variable not in dump: " + str(variable.name)
                assert(variable.shape == dump["params"][variable.name].shape), "Variable shape not as expected: " + str(variable.name) + " " + str(variable.shape) + " " + str(dump["params"][variable.name].shape)
                value = numpy.asarray(dump["params"][variable.name])
                self.session.run(variable.assign(value))
