
jasper_eval.py
! cat /tmp/nemo_asr_app/tools/NeMo/jasper_eval.py
# Copyright (c) 2019 NVIDIA Corporation
# some of the code taken from:
# https://github.com/NVIDIA/NeMo/tree/master/examples/asr
# https://github.com/NVIDIA/OpenSeq2Seq/blob/master/scripts/decode.py
import argparse
import copy
import os
import json

from ruamel.yaml import YAML
import numpy as np

import nemo
import nemo_asr
from nemo_asr.helpers import word_error_rate, post_process_predictions, \
                             post_process_transcripts
from tools.filetools import mkdir_p, rm_rf, file_exists


def main():
    parser = argparse.ArgumentParser(description='Jasper')
    # model params
    parser.add_argument("--model_config", type=str, required=True)
    parser.add_argument("--eval_datasets", type=str, required=True)
    parser.add_argument("--load_dir", type=str, required=True)
    parser.add_argument("--model_id", type=str, required=True) # mine
    # run params
    parser.add_argument("--local_rank", default=None, type=int)
    parser.add_argument("--batch_size", default=64, type=int)
    parser.add_argument("--amp_opt_level", default="O0", type=str) # mine
    # store results
    parser.add_argument("--save_results", default=None, type=str) # mine

    # lm inference parameters
    parser.add_argument("--lm_path", default=None, type=str)
    parser.add_argument(
        '--alpha', default=2., type=float,
        help='value of LM weight',
        required=False)
    parser.add_argument(
        '--alpha_max', type=float,
        help='maximum value of LM weight (for a grid search in \'eval\' mode)',
        required=False)
    parser.add_argument(
        '--alpha_step', type=float,
        help='step for LM weight\'s tuning in \'eval\' mode',
        required=False, default=0.1)
    parser.add_argument(
        '--beta', default=1.5, type=float,
        help='value of word count weight',
        required=False)
    parser.add_argument(
        '--beta_max', type=float,
        help='maximum value of word count weight (for a grid search in \
          \'eval\' mode',
        required=False)
    parser.add_argument(
        '--beta_step', type=float,
        help='step for word count weight\'s tuning in \'eval\' mode',
        required=False, default=0.1)
    parser.add_argument(
        "--beam_width", default=128, type=int)

    args = parser.parse_args()
    batch_size = args.batch_size
    load_dir = args.load_dir

    if args.local_rank is not None:
        if args.lm_path:
            raise NotImplementedError(
                "Beam search decoder with LM does not currently support "
                "evaluation on multi-gpu.")
        device = nemo.core.DeviceType.AllGpu
    else:
        device = nemo.core.DeviceType.GPU

    # Instantiate Neural Factory with supported backend
    neural_factory = nemo.core.NeuralModuleFactory(
        backend=nemo.core.Backend.PyTorch,
        local_rank=args.local_rank,
        optimization_level=args.amp_opt_level,
        placement=device)
    logger = neural_factory.logger

    if args.local_rank is not None:
        logger.info('Doing ALL GPU')

    yaml = YAML(typ="safe")
    with open(args.model_config) as f:
        jasper_params = yaml.load(f)
    vocab = jasper_params['labels']
    sample_rate = jasper_params['sample_rate']

    # single eval dataset
    eval_datasets = args.eval_datasets
    eval_dl_params = copy.deepcopy(jasper_params["AudioToTextDataLayer"])
    eval_dl_params.update(jasper_params["AudioToTextDataLayer"]["eval"])
    del eval_dl_params["train"]
    del eval_dl_params["eval"]
    data_layer = nemo_asr.AudioToTextDataLayer(
        manifest_filepath=eval_datasets,
        sample_rate=sample_rate,
        labels=vocab,
        batch_size=batch_size,
        **eval_dl_params)

    N = len(data_layer)
    logger.info('Evaluating {0} examples'.format(N))

    data_preprocessor = nemo_asr.AudioToMelSpectrogramPreprocessor(
        sample_rate=sample_rate,
        **jasper_params["AudioToMelSpectrogramPreprocessor"])
    jasper_encoder = nemo_asr.JasperEncoder(
        feat_in=jasper_params["AudioToMelSpectrogramPreprocessor"]["features"],
        **jasper_params["JasperEncoder"])
    jasper_decoder = nemo_asr.JasperDecoderForCTC(
        feat_in=jasper_params["JasperEncoder"]["jasper"][-1]["filters"],
        num_classes=len(vocab))
    greedy_decoder = nemo_asr.GreedyCTCDecoder()

    logger.info('================================')
    logger.info(
        f"Number of parameters in encoder: {jasper_encoder.num_weights}")
    logger.info(
        f"Number of parameters in decoder: {jasper_decoder.num_weights}")
    logger.info(
        f"Total number of parameters: "
        f"{jasper_decoder.num_weights + jasper_encoder.num_weights}")
    logger.info('================================')

    # Define inference DAG
    audio_signal_e1, a_sig_length_e1, transcript_e1, transcript_len_e1 =\
        data_layer()
    processed_signal_e1, p_length_e1 = data_preprocessor(
        input_signal=audio_signal_e1,
        length=a_sig_length_e1)
    encoded_e1, encoded_len_e1 = jasper_encoder(
        audio_signal=processed_signal_e1,
        length=p_length_e1)
    log_probs_e1 = jasper_decoder(encoder_output=encoded_e1)
    predictions_e1 = greedy_decoder(log_probs=log_probs_e1)

    eval_tensors = [log_probs_e1, predictions_e1,
                    transcript_e1, transcript_len_e1, encoded_len_e1]

    # inference
    evaluated_tensors = neural_factory.infer(
        tensors=eval_tensors,
        checkpoint_dir=load_dir,
        cache=True
    )

    greedy_hypotheses = post_process_predictions(evaluated_tensors[1], vocab)
    references = post_process_transcripts(
        evaluated_tensors[2], evaluated_tensors[3], vocab)

    wer = word_error_rate(hypotheses=greedy_hypotheses, references=references)
    logger.info("Greedy WER {:.2f}%".format(wer*100))

    # language model
    if args.lm_path:
        if args.alpha_max is None:
            args.alpha_max = args.alpha
        # include alpha_max in tuning range
        args.alpha_max += args.alpha_step/10.0

        if args.beta_max is None:
            args.beta_max = args.beta
        # include beta_max in tuning range
        args.beta_max += args.beta_step/10.0

        beam_wers = []

        for alpha in np.arange(args.alpha, args.alpha_max, args.alpha_step):
            for beta in np.arange(args.beta, args.beta_max, args.beta_step):
                logger.info('================================')
                logger.info(f'Infering with (alpha, beta): ({alpha}, {beta})')
                beam_search_with_lm = nemo_asr.BeamSearchDecoderWithLM(
                    vocab=vocab,
                    beam_width=args.beam_width,
                    alpha=alpha,
                    beta=beta,
                    lm_path=args.lm_path,
                    num_cpus=max(os.cpu_count(), 1))
                beam_predictions_e1 = beam_search_with_lm(
                    log_probs=log_probs_e1, log_probs_length=encoded_len_e1)

                evaluated_tensors = neural_factory.infer(
                    tensors=[beam_predictions_e1],
                    use_cache=True,
                    verbose=False
                )

                beam_hypotheses = []
                # Over mini-batch
                for i in evaluated_tensors[-1]:
                    # Over samples
                    for j in i:
                        beam_hypotheses.append(j[0][1])
                lm_wer = word_error_rate(
                    hypotheses=beam_hypotheses, references=references)
                logger.info("Beam WER {:.2f}%".format(lm_wer*100))
                beam_wers.append(((alpha, beta), lm_wer*100))

        logger.info('Beam WER for (alpha, beta)')
        logger.info('================================')
        logger.info('\n' + '\n'.join([str(e) for e in beam_wers]))
        logger.info('================================')
        best_beam_wer = min(beam_wers, key=lambda x: x[1])
        logger.info('Best (alpha, beta): '
                    f'{best_beam_wer[0]}, '
                    f'WER: {best_beam_wer[1]:.2f}%')


    # save results
    if args.save_results:
        selected_dataset = args.eval_datasets
        results = {
          "model_id": args.model_id,
          "dataset": selected_dataset,
          "wer": wer,
          "transcript": ' '.join(greedy_hypotheses),
          "gtruth": ' '.join(references)
        }
        if args.lm_path:
            results['alpha-beta'] = best_beam_wer[0]
            results['beam transcript'] = ' '.join(beam_hypotheses)
            results['lm_wer'] = best_beam_wer[1]/100
        else:
            results['lm_wer'] = None

        mkdir_p(args.save_results)

        dataset_name = selected_dataset.split("/")[-1].split(".")[0]
        model_name = args.model_id
        inf_type = "lm" if args.lm_path else "am"
        if args.alpha_step:
          inf_type = inf_type + "_grid"
        filename = os.path.join(args.save_results,
                                "results-" + inf_type + "__" \
                                + dataset_name + "__" + model_name + ".json")
        logger.info("Saving inference results to {}".format(filename))
        with open(filename, "w") as out_file:
            json.dump(results, out_file)
if __name__ == "__main__":
    main()