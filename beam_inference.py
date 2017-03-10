from __future__ import absolute_import
from __future__ import division
from __future__ import print_function

from tensorflow.contrib.layers.python.layers import layers
from tensorflow.contrib.rnn.python.ops import core_rnn_cell_impl
from tensorflow.python.framework import dtypes
from tensorflow.python.framework import function
from tensorflow.python.framework import ops
from tensorflow.python.ops import array_ops
from tensorflow.python.ops import control_flow_ops
from tensorflow.python.ops import gen_data_flow_ops
from tensorflow.python.ops import tensor_array_ops
from tensorflow.python.ops import math_ops
from tensorflow.python.ops import nn_ops
from tensorflow.python.ops import variable_scope
from tensorflow.python.util import nest

def attention_decoder_fn_beam_inference(output_fn,
                                       encoder_state,
                                       attention_keys,
                                       attention_values,
                                       attention_score_fn,
                                       attention_construct_fn,
                                       embeddings,
                                       start_of_sequence_id,
                                       end_of_sequence_id,
                                       maximum_length,
                                       num_decoder_symbols,
                                       beam_size,
                                       remove_unk=False,
                                       beam_diverse=False,
                                       dtype=dtypes.int32,
                                       name=None):
    """Attentional decoder function for `dynamic_rnn_decoder` during inference.
    The `attention_decoder_fn_inference` is a simple inference function for a
    sequence-to-sequence model. It should be used when `dynamic_rnn_decoder` is
    in the inference mode.
    The `attention_decoder_fn_inference` is called with user arguments
    and returns the `decoder_fn`, which can be passed to the
    `dynamic_rnn_decoder`, such that
    ```
    dynamic_fn_inference = attention_decoder_fn_inference(...)
    outputs_inference, state_inference = dynamic_rnn_decoder(
            decoder_fn=dynamic_fn_inference, ...)
    ```
    Further usage can be found in the `kernel_tests/seq2seq_test.py`.
    Args:
        output_fn: An output function to project your `cell_output` onto class
        logits.
        An example of an output function;
        ```
            tf.variable_scope("decoder") as varscope
                output_fn = lambda x: layers.linear(x, num_decoder_symbols,
                                                                                        scope=varscope)
                outputs_train, state_train = seq2seq.dynamic_rnn_decoder(...)
                logits_train = output_fn(outputs_train)
                varscope.reuse_variables()
                logits_inference, state_inference = seq2seq.dynamic_rnn_decoder(
                        output_fn=output_fn, ...)
        ```
        If `None` is supplied it will act as an identity function, which
        might be wanted when using the RNNCell `OutputProjectionWrapper`.
        encoder_state: The encoded state to initialize the `dynamic_rnn_decoder`.
        attention_keys: to be compared with target states.
        attention_values: to be used to construct context vectors.
        attention_score_fn: to compute similarity between key and target states.
        attention_construct_fn: to build attention states.
        embeddings: The embeddings matrix used for the decoder sized
        `[num_decoder_symbols, embedding_size]`.
        start_of_sequence_id: The start of sequence ID in the decoder embeddings.
        end_of_sequence_id: The end of sequence ID in the decoder embeddings.
        maximum_length: The maximum allowed of time steps to decode.
        num_decoder_symbols: The number of classes to decode at each time step.
        dtype: (default: `dtypes.int32`) The default data type to use when
        handling integer objects.
        name: (default: `None`) NameScope for the decoder function;
            defaults to "attention_decoder_fn_inference"
    Returns:
        A decoder function with the required interface of `dynamic_rnn_decoder`
        intended for inference.
    """
    with ops.name_scope(name, "attention_decoder_fn_inference", [
            output_fn, encoder_state, attention_keys, attention_values,
            attention_score_fn, attention_construct_fn, embeddings,
            start_of_sequence_id, end_of_sequence_id, maximum_length,
            num_decoder_symbols, dtype
    ]):
        start_of_sequence_id = ops.convert_to_tensor(start_of_sequence_id, dtype)
        end_of_sequence_id = ops.convert_to_tensor(end_of_sequence_id, dtype)
        maximum_length = ops.convert_to_tensor(maximum_length, dtype)
        beam_size = ops.convert_to_tensor(beam_size, dtype)
        num_decoder_symbols = ops.convert_to_tensor(num_decoder_symbols, dtype)
        encoder_info = nest.flatten(encoder_state)[0]
        batch_size = encoder_info.get_shape()[0].value
        if output_fn is None:
            output_fn = lambda x: x
        if batch_size is None:
            batch_size = array_ops.shape(encoder_info)[0]

    def decoder_fn(time, cell_state, cell_input, cell_output, context_state):
        """Decoder function used in the `dynamic_rnn_decoder` for inference.
        The main difference between this decoder function and the `decoder_fn` in
        `attention_decoder_fn_train` is how `next_cell_input` is calculated. In
        decoder function we calculate the next input by applying an argmax across
        the feature dimension of the output from the decoder. This is a
        greedy-search approach. (Bahdanau et al., 2014) & (Sutskever et al., 2014)
        use beam-search instead.
        Args:
            time: positive integer constant reflecting the current timestep.
            cell_state: state of RNNCell.
            cell_input: input provided by `dynamic_rnn_decoder`.
            cell_output: output of RNNCell.
            context_state: context state provided by `dynamic_rnn_decoder`.
        Returns:
            A tuple (done, next state, next input, emit output, next context state)
            where:
            done: A boolean vector to indicate which sentences has reached a
            `end_of_sequence_id`. This is used for early stopping by the
            `dynamic_rnn_decoder`. When `time>=maximum_length` a boolean vector with
            all elements as `true` is returned.
            next state: `cell_state`, this decoder function does not modify the
            given state.
            next input: The embedding from argmax of the `cell_output` is used as
            `next_input`.
            emit output: If `output_fn is None` the supplied `cell_output` is
            returned, else the `output_fn` is used to update the `cell_output`
            before calculating `next_input` and returning `cell_output`.
            next context state: `context_state`, this decoder function does not
            modify the given context state. The context state could be modified when
            applying e.g. beam search.
        Raises:
            ValueError: if cell_input is not None.
        """
        with ops.name_scope(
                name, "attention_decoder_fn_inference",
                [time, cell_state, cell_input, cell_output, context_state]):
            if cell_input is not None:
                raise ValueError("Expected cell_input to be None, but saw: %s" %
                                                 cell_input)
            if cell_output is None:
                # invariant that this is time == 0
                next_input_id = array_ops.ones(
                        [batch_size,], dtype=dtype) * (start_of_sequence_id)
                done = array_ops.zeros([batch_size,], dtype=dtypes.bool)
                cell_state = encoder_state
                cell_output = array_ops.zeros(
                        [num_decoder_symbols], dtype=dtypes.float32)
                cell_input = array_ops.gather(embeddings, next_input_id)

                # init attention
                attention = _init_attention(encoder_state)
                # init context state
                log_beam_probs = tensor_array_ops.TensorArray(dtype=dtypes.float32, tensor_array_name="log_beam_probs", size=maximum_length, dynamic_size=True, infer_shape=False)
                beam_parents = tensor_array_ops.TensorArray(dtype=dtypes.int32, tensor_array_name="beam_parents", size=maximum_length, dynamic_size=True, infer_shape=False)
                beam_symbols = tensor_array_ops.TensorArray(dtype=dtypes.int32, tensor_array_name="beam_symbols", size=maximum_length, dynamic_size=True, infer_shape=False)
                result_probs = tensor_array_ops.TensorArray(dtype=dtypes.float32, tensor_array_name="result_probs", size=maximum_length, dynamic_size=True, infer_shape=False)
                result_parents = tensor_array_ops.TensorArray(dtype=dtypes.int32, tensor_array_name="result_parents", size=maximum_length, dynamic_size=True, infer_shape=False)
                result_symbols = tensor_array_ops.TensorArray(dtype=dtypes.int32, tensor_array_name="result_symbols", size=maximum_length, dynamic_size=True, infer_shape=False)
                context_state = (log_beam_probs, beam_parents, beam_symbols, result_probs, result_parents, result_symbols)
            else:
                # construct attention
                attention = attention_construct_fn(cell_output, attention_keys,
                        attention_values)
                cell_output = attention

                # beam search decoder
                (log_beam_probs, beam_parents, beam_symbols, result_probs, result_parents, result_symbols) = context_state
                
                cell_output = output_fn(cell_output)    # logits
                cell_output = nn_ops.softmax(cell_output)
                
                delta = 0.0

                if remove_unk:
                    cell_output = array_ops.split(cell_output, [2, num_decoder_symbols-2], 1)[1]
                    
                    probs = control_flow_ops.cond(
                            math_ops.equal(time, ops.convert_to_tensor(1, dtype)),
                            lambda: math_ops.log(cell_output[0]+ops.convert_to_tensor(1e-20, dtypes.float32)),
                            lambda: math_ops.log(cell_output+ops.convert_to_tensor(1e-20, dtypes.float32)) + array_ops.reshape(log_beam_probs.read(time-2), [-1, 1]))
                    
                    probs_sort, indices_sort = nn_ops.top_k(probs, num_decoder_symbols-2)
                    penalty = math_ops.cast(math_ops.range(1, num_decoder_symbols-1), dtypes.float32) * delta
                    probs = array_ops.reshape(probs_sort-penalty, [-1])
                    indices_sort = array_ops.reshape(indices_sort, [-1])

                    best_probs, indices = nn_ops.top_k(probs, beam_size * 2)

                    parents = indices // (num_decoder_symbols - 2)
                    indices = array_ops.gather(indices_sort, indices)

                    symbols = indices + 2
                else:
                    probs = control_flow_ops.cond(
                            math_ops.equal(time, ops.convert_to_tensor(1, dtype)),
                            lambda: array_ops.reshape(math_ops.log(cell_output[0]+ops.convert_to_tensor(1e-20, dtypes.float32)), [-1, num_decoder_symbols]),
                            lambda: array_ops.reshape(math_ops.log(cell_output+ops.convert_to_tensor(1e-20, dtypes.float32)) + array_ops.reshape(log_beam_probs.read(time-2), [-1, 1]), [-1, beam_size * num_decoder_symbols]))
                   
                    best_probs, indices = nn_ops.top_k(probs, beam_size * 2)
                    indices = array_ops.reshape(indices, [-1])
                    best_probs = array_ops.reshape(best_probs, [-1])

                    symbols = indices % num_decoder_symbols

                    parents = indices // num_decoder_symbols 
#                if remove_unk:
#                    cell_output = array_ops.split(cell_output, [2, num_decoder_symbols-2], 1)[1]
#                
#                    probs = control_flow_ops.cond(
#                            math_ops.equal(time, ops.convert_to_tensor(1, dtype)),
#                            lambda: array_ops.reshape(math_ops.log(cell_output[0]+ops.convert_to_tensor(1e-20, dtypes.float32)), [-1, num_decoder_symbols-2]),
#                            lambda: array_ops.reshape(math_ops.log(cell_output+ops.convert_to_tensor(1e-20, dtypes.float32)) + array_ops.reshape(log_beam_probs.read(time-2), [-1, 1]), [-1, beam_size * (num_decoder_symbols-2)]))
#                   
#                    best_probs, indices = nn_ops.top_k(probs, beam_size * 2)
#                    indices = array_ops.reshape(indices, [-1])
#                    best_probs = array_ops.reshape(best_probs, [-1])
#
#                    symbols = indices % (num_decoder_symbols - 2)
#                    symbols = symbols + 2
#                    parents = indices // (num_decoder_symbols - 2)
#                else:
#                    probs = control_flow_ops.cond(
#                            math_ops.equal(time, ops.convert_to_tensor(1, dtype)),
#                            lambda: array_ops.reshape(math_ops.log(cell_output[0]+ops.convert_to_tensor(1e-20, dtypes.float32)), [-1, num_decoder_symbols]),
#                            lambda: array_ops.reshape(math_ops.log(cell_output+ops.convert_to_tensor(1e-20, dtypes.float32)) + array_ops.reshape(log_beam_probs.read(time-2), [-1, 1]), [-1, beam_size * num_decoder_symbols]))
#                   
#                    best_probs, indices = nn_ops.top_k(probs, beam_size * 2)
#                    indices = array_ops.reshape(indices, [-1])
#                    best_probs = array_ops.reshape(best_probs, [-1])
#
#                    symbols = indices % num_decoder_symbols
#
#                    parents = indices // num_decoder_symbols 

                partition = math_ops.cast(math_ops.cast(symbols-end_of_sequence_id, dtypes.bool), dtypes.int32)

                probs_group = gen_data_flow_ops.dynamic_partition(best_probs, partition, 2)
                symbols_group = gen_data_flow_ops.dynamic_partition(symbols, partition, 2)
                parents_group = gen_data_flow_ops.dynamic_partition(parents, partition, 2)
                

                _probs = probs_group[1][:beam_size]
                _symbols = symbols_group[1][:beam_size]
                _parents = parents_group[1][:beam_size]


                log_beam_probs = log_beam_probs.write(time-1, _probs)
                beam_symbols = beam_symbols.write(time-1, _symbols)
                beam_parents = beam_parents.write(time-1, _parents)
                result_probs = result_probs.write(time-1, best_probs)
                result_symbols = result_symbols.write(time-1, symbols)
                result_parents = result_parents.write(time-1, parents)


                next_input_id = _symbols

                state_size = int(cell_state[0].get_shape().with_rank(2)[1])
                attn_size = int(attention.get_shape().with_rank(2)[1])
                state = []
                for j in cell_state:
                    state.append(array_ops.reshape(array_ops.gather(j, _parents), [-1, state_size]))
                cell_state = tuple(state)
                attention = array_ops.reshape(array_ops.gather(attention, _parents), [-1, attn_size])

                done = math_ops.equal(next_input_id, end_of_sequence_id)
                cell_input = array_ops.gather(embeddings, next_input_id)

            # combine cell_input and attention
            next_input = array_ops.concat([cell_input, attention], 1)

            # if time > maxlen, return all true vector
            done = control_flow_ops.cond(
                    math_ops.greater(time, maximum_length),
                    lambda: array_ops.ones([batch_size,], dtype=dtypes.bool),
                    lambda: array_ops.zeros([batch_size,], dtype=dtypes.bool))
            return (done, cell_state, next_input, cell_output, (log_beam_probs, beam_parents, beam_symbols, result_probs, result_parents, result_symbols))#context_state)

    return decoder_fn

## Helper functions ##
def prepare_attention(attention_states,
                          attention_option,
                          num_units,
                          reuse=False):
    """Prepare keys/values/functions for attention.
    Args:
        attention_states: hidden states to attend over.
        attention_option: how to compute attention, either "luong" or "bahdanau".
        num_units: hidden state dimension.
        reuse: whether to reuse variable scope.
    Returns:
        attention_keys: to be compared with target states.
        attention_values: to be used to construct context vectors.
        attention_score_fn: to compute similarity between key and target states.
        attention_construct_fn: to build attention states.
    """

    # Prepare attention keys / values from attention_states
    with variable_scope.variable_scope("attention_keys", reuse=reuse) as scope:
        attention_keys = layers.linear(
            attention_states, num_units, biases_initializer=None, scope=scope)
    attention_values = attention_states

    # Attention score function
    attention_score_fn = _create_attention_score_fn("attention_score", num_units,
                                                        attention_option, reuse)

    # Attention construction function
    attention_construct_fn = _create_attention_construct_fn("attention_construct",
                                  num_units,
                                  attention_score_fn,
                                  reuse)

    return (attention_keys, attention_values, attention_score_fn,
                    attention_construct_fn)


def _init_attention(encoder_state):
    """Initialize attention. Handling both LSTM and GRU.
    Args:
        encoder_state: The encoded state to initialize the `dynamic_rnn_decoder`.
    Returns:
        attn: initial zero attention vector.
    """

    # Multi- vs single-layer
    # TODO(thangluong): is this the best way to check?
    if isinstance(encoder_state, tuple):
        top_state = encoder_state[-1]
    else:
        top_state = encoder_state

    # LSTM vs GRU
    if isinstance(top_state, core_rnn_cell_impl.LSTMStateTuple):
        attn = array_ops.zeros_like(top_state.h)
    else:
        attn = array_ops.zeros_like(top_state)

    return attn


def _create_attention_construct_fn(name, num_units, attention_score_fn, reuse):
    """Function to compute attention vectors.
    Args:
        name: to label variables.
        num_units: hidden state dimension.
        attention_score_fn: to compute similarity between key and target states.
        reuse: whether to reuse variable scope.
    Returns:
        attention_construct_fn: to build attention states.
    """
    with variable_scope.variable_scope(name, reuse=reuse) as scope:

        def construct_fn(attention_query, attention_keys, attention_values):
            context = attention_score_fn(attention_query, attention_keys,
                                                                     attention_values)
            concat_input = array_ops.concat([attention_query, context], 1)
            attention = layers.linear(
                    concat_input, num_units, biases_initializer=None, scope=scope)
            return attention

        return construct_fn


# keys: [batch_size, attention_length, attn_size]
# query: [batch_size, 1, attn_size]
# return weights [batch_size, attention_length]
@function.Defun(func_name="attn_add_fun", noinline=True)
def _attn_add_fun(v, keys, query):
    return math_ops.reduce_sum(v * math_ops.tanh(keys + query), [2])


@function.Defun(func_name="attn_mul_fun", noinline=True)
def _attn_mul_fun(keys, query):
    return math_ops.reduce_sum(keys * query, [2])


def _create_attention_score_fn(name,
                                   num_units,
                                   attention_option,
                                   reuse,
                                   dtype=dtypes.float32):
    """Different ways to compute attention scores.
    Args:
        name: to label variables.
        num_units: hidden state dimension.
        attention_option: how to compute attention, either "luong" or "bahdanau".
            "bahdanau": additive (Bahdanau et al., ICLR'2015)
            "luong": multiplicative (Luong et al., EMNLP'2015)
        reuse: whether to reuse variable scope.
        dtype: (default: `dtypes.float32`) data type to use.
    Returns:
        attention_score_fn: to compute similarity between key and target states.
    """
    with variable_scope.variable_scope(name, reuse=reuse):
        if attention_option == "bahdanau":
            query_w = variable_scope.get_variable(
                    "attnW", [num_units, num_units], dtype=dtype)
            score_v = variable_scope.get_variable("attnV", [num_units], dtype=dtype)

        def attention_score_fn(query, keys, values):
            """Put attention masks on attention_values using attention_keys and query.
            Args:
                query: A Tensor of shape [batch_size, num_units].
                keys: A Tensor of shape [batch_size, attention_length, num_units].
                values: A Tensor of shape [batch_size, attention_length, num_units].
            Returns:
                context_vector: A Tensor of shape [batch_size, num_units].
            Raises:
                ValueError: if attention_option is neither "luong" or "bahdanau".
            """
            if attention_option == "bahdanau":
                # transform query
                query = math_ops.matmul(query, query_w)

                # reshape query: [batch_size, 1, num_units]
                query = array_ops.reshape(query, [-1, 1, num_units])

                # attn_fun
                scores = _attn_add_fun(score_v, keys, query)
            elif attention_option == "luong":
                # reshape query: [batch_size, 1, num_units]
                query = array_ops.reshape(query, [-1, 1, num_units])

                # attn_fun
                scores = _attn_mul_fun(keys, query)
            else:
                raise ValueError("Unknown attention option %s!" % attention_option)

            # Compute alignment weights
            #     scores: [batch_size, length]
            #     alignments: [batch_size, length]
            # TODO(thangluong): not normalize over padding positions.
            alignments = nn_ops.softmax(scores)

            # Now calculate the attention-weighted vector.
            alignments = array_ops.expand_dims(alignments, 2)
            context_vector = math_ops.reduce_sum(alignments * values, [1])
            context_vector.set_shape([None, num_units])

            return context_vector

        return attention_score_fn