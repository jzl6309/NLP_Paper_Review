import tensorflow as tf
from tensorflow.python.ops import tensor_array_ops, control_flow_ops
import numpy as np

class Generator(object):
    def __init__(self, num_emb, batch_size, emb_dim, hidden_dim,
                 sequence_length, start_token, mid_layer,
                 learning_rate=0.005):
        self.num_emb = num_emb
        self.batch_size = batch_size
        self.emb_dim = emb_dim
        self.hidden_dim = hidden_dim
        self.sequence_length = sequence_length
        self.start_token = tf.constant([start_token] * self.batch_size, dtype=tf.int32)
        self.learning_rate = tf.Variable(float(learning_rate), trainable=False)
        self.g_params = []
        self.grad_clip = 5.0
        self.mid_layer = mid_layer

        self.expected_reward = tf.Variable(tf.zeros([self.sequence_length]))

        with tf.variable_scope('generator'):
            self.g_embeddings = tf.Variable(self.init_matrix([self.num_emb, self.emb_dim]))
            self.g_params.append(self.g_embeddings)
            self.g_recurrent_unit = self.create_recurrent_unit(self.g_params)  # maps h_tm1 to h_t for generator
            self.g_output_unit = self.create_output_unit(self.g_params, mid_layer)  # maps h_t to o_t (output token logits)

        # placeholder definition
        self.x = tf.placeholder(tf.int32, shape=[self.batch_size, self.sequence_length]) # sequence of tokens generated by generator
        self.off_policy_prob = tf.placeholder(tf.float32, shape=[self.batch_size, self.sequence_length], name='off_policy_prob')
        self.baseline = tf.placeholder(tf.float32, shape=[self.sequence_length], name='baseline')
        self.rewards = tf.placeholder(tf.float32, shape=[self.batch_size, self.sequence_length], name='rewards')
        self.decay_weight = tf.placeholder(tf.float32, name="decay_weight")

        # processed for batch
        # self.f_weight = tf.Print(self.f_weight, [self.f_weight[-3:]], message='='*10)
        with tf.device("/cpu:0"):
            self.word = tf.nn.embedding_lookup(self.g_embeddings, self.x)
            self.processed_x = tf.transpose(self.word, perm=[1, 0, 2])  # seq_length x batch_size x emb_dim

        # Initial states
        self.h0 = tf.zeros([self.batch_size, self.hidden_dim])
        self.h0 = tf.stack([self.h0, self.h0])

        gen_o = tensor_array_ops.TensorArray(dtype=tf.float32, size=self.sequence_length,
                                             dynamic_size=False, infer_shape=True)
        gen_x = tensor_array_ops.TensorArray(dtype=tf.int32, size=self.sequence_length,
                                             dynamic_size=False, infer_shape=True)
        gen_h = tensor_array_ops.TensorArray(dtype=tf.float32, size=self.sequence_length,
                                             dynamic_size=False, infer_shape=True)

        def _g_recurrence(i, x_t, h_tm1, gen_o, gen_x):
            h_t = self.g_recurrent_unit(x_t, h_tm1)  # hidden_memory_tuple
            o_t = self.g_output_unit(h_t)  # batch x vocab , logits not prob
            log_prob = tf.log(tf.nn.softmax(o_t))
            next_token = tf.cast(tf.reshape(tf.multinomial(log_prob, 1), [self.batch_size]), tf.int32)
            x_tp1 = tf.nn.embedding_lookup(self.g_embeddings, next_token)  # batch x emb_dim
            gen_o = gen_o.write(i, tf.reduce_sum(tf.multiply(tf.one_hot(next_token, self.num_emb, 1.0, 0.0),
                                                             tf.nn.softmax(o_t)), 1))  # [batch_size] , prob
            gen_x = gen_x.write(i, next_token)  # indices, batch_size
            return i + 1, x_tp1, h_t, gen_o, gen_x

        _, _, _, self.gen_o, self.gen_x = control_flow_ops.while_loop(
            cond=lambda i, _1, _2, _3, _4: i < self.sequence_length,
            body=_g_recurrence,
            loop_vars=(tf.constant(0, dtype=tf.int32),
                       tf.nn.embedding_lookup(self.g_embeddings, self.start_token), self.h0, gen_o, gen_x))

        self.gen_x = self.gen_x.stack()  # seq_length x batch_size
        self.gen_x = tf.transpose(self.gen_x, perm=[1, 0])  # batch_size x seq_length

        # supervised pretraining for generator
        g_predictions = tensor_array_ops.TensorArray(
            dtype=tf.float32, size=self.sequence_length,
            dynamic_size=False, infer_shape=True)

        ta_emb_x = tensor_array_ops.TensorArray(
            dtype=tf.float32, size=self.sequence_length)
        ta_emb_x = ta_emb_x.unstack(self.processed_x)

        def _pretrain_recurrence(i, x_t, h_tm1, g_predictions, gen_h):
            gen_h = gen_h.write(i, tf.unstack(h_tm1)[0])
            h_t = self.g_recurrent_unit(x_t, h_tm1)
            o_t = self.g_output_unit(h_t)
            g_predictions = g_predictions.write(i, tf.nn.softmax(o_t))  # batch x vocab_size
            x_tp1 = ta_emb_x.read(i)
            return i + 1, x_tp1, h_t, g_predictions, gen_h

        _, _, _, self.g_predictions, self.gen_h = control_flow_ops.while_loop(
            cond=lambda i, _1, _2, _3, _4: i < self.sequence_length,
            body=_pretrain_recurrence,
            loop_vars=(tf.constant(0, dtype=tf.int32),
                       tf.nn.embedding_lookup(self.g_embeddings, self.start_token),
                       self.h0, g_predictions, gen_h))

        # CalculateMean cross-entropy loss
        self.g_predictions = tf.transpose(self.g_predictions.stack(), perm=[1, 0, 2])  # batch_size x seq_length x vocab_size

        # self.log_pred = tf.one_hot(tf.to_int32(tf.reshape(self.x, [-1])), self.num_emb, 1.0, 0.0) * \
        #                 tf.log(tf.reshape(self.g_predictions, [-1, self.num_emb]))
        # clip_log_pred & log_pred :  batch*seq  x vocab_size
        self.clipped_log_pred = tf.one_hot(tf.to_int32(tf.reshape(self.x, [-1])), self.num_emb, 1.0, 0.0) * tf.log(
            tf.clip_by_value(tf.reshape(self.g_predictions, [-1, self.num_emb]), 1e-20, 1.0))

        self.sent_log = tf.reduce_sum(tf.reshape(tf.reduce_sum(self.clipped_log_pred, -1), [self.batch_size, self.sequence_length]), axis=1)
        # pretraining loss
        self.pretrain_loss = -tf.reduce_sum(self.clipped_log_pred) / (self.sequence_length * self.batch_size)

        # training updates
        pretrain_opt = self.optimizer(self.learning_rate)

        self.pretrain_grad, _ = tf.clip_by_global_norm(tf.gradients(self.pretrain_loss, self.g_params), self.grad_clip)
        self.pretrain_updates = pretrain_opt.apply_gradients(zip(self.pretrain_grad, self.g_params))

        #######################################################################################################
        #  Unsupervised Training
        #######################################################################################################
        log_pred = tf.reduce_sum(self.clipped_log_pred, -1)
        # log_pred: batch * seq (1 dim)
        bz_log_pred = tf.reshape(log_pred, [self.batch_size, self.sequence_length])
        #sig_bz_log_pred = tf.nn.sigmoid(tf.reshape(log_pred, [self.batch_size, self.sequence_length]))
        sig_bz_log_pred = tf.reshape(log_pred, [self.batch_size, self.sequence_length])
        accumlated_pred = tf.matmul(sig_bz_log_pred, tf.constant(np.tri(self.sequence_length), dtype=tf.float32))
        accumlated_pred = tf.stop_gradient(accumlated_pred)

        ratio = tf.exp(bz_log_pred - self.off_policy_prob)
        # ratio = tf.Print(ratio, [ratio[:2]], message='*'*10, summarize=100)
        clipped_ratio = tf.clip_by_value(ratio, 0.8, 1.2)
        choice_a = ratio * (self.rewards - accumlated_pred * self.decay_weight - self.baseline)
        choice_b = clipped_ratio * (self.rewards - accumlated_pred * self.decay_weight - self.baseline)
        self.g_loss = - tf.reduce_mean(tf.minimum(choice_a, choice_b))

        g_opt = self.optimizer(self.learning_rate)

        self.g_grad, _ = tf.clip_by_global_norm(tf.gradients(self.g_loss, self.g_params), self.grad_clip)
        self.g_updates = g_opt.apply_gradients(zip(self.g_grad, self.g_params))

    def generate(self, sess):
        outputs = sess.run(self.gen_x)
        return outputs

    def pretrain_step(self, sess, x):
        outputs = sess.run([self.pretrain_updates, self.pretrain_loss], feed_dict={self.x: x})
        return outputs

    def rl_train_step(self, sess, x, rewards, baseline, offpolicy, decay_weight):
        outputs = sess.run([self.g_updates, self.g_loss], feed_dict={self.x:x, self.rewards: rewards,
                                                                     self.baseline:baseline, self.off_policy_prob:offpolicy,
                                                                     self.decay_weight: decay_weight})
        return outputs

    def init_matrix(self, shape):
        return tf.random_normal(shape, stddev=0.1)

    def init_vector(self, shape):
        return tf.zeros(shape)

    def create_recurrent_unit(self, params):
        # Weights and Bias for input and hidden tensor
        self.Wi = tf.Variable(self.init_matrix([self.emb_dim, self.hidden_dim]))
        self.Ui = tf.Variable(self.init_matrix([self.hidden_dim, self.hidden_dim]))
        self.bi = tf.Variable(self.init_matrix([self.hidden_dim]))

        self.Wf = tf.Variable(self.init_matrix([self.emb_dim, self.hidden_dim]))
        self.Uf = tf.Variable(self.init_matrix([self.hidden_dim, self.hidden_dim]))
        self.bf = tf.Variable(self.init_matrix([self.hidden_dim]))

        self.Wog = tf.Variable(self.init_matrix([self.emb_dim, self.hidden_dim]))
        self.Uog = tf.Variable(self.init_matrix([self.hidden_dim, self.hidden_dim]))
        self.bog = tf.Variable(self.init_matrix([self.hidden_dim]))

        self.Wc = tf.Variable(self.init_matrix([self.emb_dim, self.hidden_dim]))
        self.Uc = tf.Variable(self.init_matrix([self.hidden_dim, self.hidden_dim]))
        self.bc = tf.Variable(self.init_matrix([self.hidden_dim]))
        params.extend([
            self.Wi, self.Ui, self.bi,
            self.Wf, self.Uf, self.bf,
            self.Wog, self.Uog, self.bog,
            self.Wc, self.Uc, self.bc])

        def unit(x, hidden_memory_tm1):
            previous_hidden_state, c_prev = tf.unstack(hidden_memory_tm1)

            # Input Gate
            i = tf.sigmoid(
                tf.matmul(x, self.Wi) +
                tf.matmul(previous_hidden_state, self.Ui) + self.bi
            )

            # Forget Gate
            f = tf.sigmoid(
                tf.matmul(x, self.Wf) +
                tf.matmul(previous_hidden_state, self.Uf) + self.bf
            )

            # Output Gate
            o = tf.sigmoid(
                tf.matmul(x, self.Wog) +
                tf.matmul(previous_hidden_state, self.Uog) + self.bog
            )

            # New Memory Cell
            c_ = tf.nn.tanh(
                tf.matmul(x, self.Wc) +
                tf.matmul(previous_hidden_state, self.Uc) + self.bc
            )

            # Final Memory cell
            c = f * c_prev + i * c_

            # Current Hidden state
            current_hidden_state = o * tf.nn.tanh(c)

            return tf.stack([current_hidden_state, c])

        return unit

    def create_output_unit(self, params, midlayer):
        self.Wbo_list = []
        midlayer.insert(0, self.hidden_dim)
        midlayer.append(self.num_emb)
        assert len(midlayer) >= 2
        for i in xrange(1, len(midlayer)):
            print i
            self.Wbo_list.append(tf.Variable(self.init_matrix([midlayer[i - 1], midlayer[i]])))
            self.Wbo_list.append(tf.Variable(self.init_matrix([midlayer[i]])))

        params.extend(self.Wbo_list)

        def unit(hidden_memory_tuple):
            hidden_state, c_prev = tf.unstack(hidden_memory_tuple)
            # hidden_state : batch x hidden_dim
            assert len(self.Wbo_list) == 2 * (len(midlayer) - 1), 'wbo is {} and midlayer is {}'.format(len(self.Wbo_list), len(midlayer))
            for j in range(len(self.Wbo_list) // 2 - 1):
                hidden_state = tf.nn.relu(tf.nn.xw_plus_b(hidden_state, self.Wbo_list[2 * j], self.Wbo_list[2 * j + 1]))
            logits = tf.nn.xw_plus_b(hidden_state, self.Wbo_list[-2], self.Wbo_list[-1])
            # output = tf.nn.softmax(logits)
            return logits

        return unit

    def optimizer(self, *args, **kwargs):
        return tf.train.AdamOptimizer(*args, **kwargs)
