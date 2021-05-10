python3 trainEBM.py \
  --ddp \
  --nodes 1 \
  --gpus 4 \
  --nr 0 \
  --port 42213 \
  --model_name="TransformerEBM" \
  --rxn_smis_file_prefix="50k_clean_rxnsmi_noreagent_canon" \
  --onthefly \
  --do_finetune \
  --do_test \
  --do_get_energies_and_acc \
	--log_file=s2e_retrosim/3x256_4h_512f_256e_CLS_256seq_SIM_log_50top200max_lr2e3w0_fac60_pat2_stop6_100ep_4GPU_bsz8_${SLURM_JOBID}.log \
	--expt_name=3x256_4h_512f_256e_CLS_256seq_SIM_log_50top200max_lr2e3w0_fac60_pat2_stop6_100ep_4GPU_bsz8 \
    --vocab_file="vocab.txt" \
    --proposals_csv_file_prefix="retrosim_200topk_200maxk_noGT" \
	--precomp_file_prefix="" \
	--representation="smiles" \
    --max_seq_len 256 \
    --encoder_embed_size 256 \
    --encoder_depth 3 \
    --encoder_hidden_size 256 \
    --encoder_num_heads 4 \
    --encoder_filter_size 512 \
    --encoder_dropout 0.05 \
    --attention_dropout 0.025 \
    --s2e_pool_type 'CLS' \
	--random_seed 0 \
    --loss_type 'log' \
	--batch_size 8 \
    --batch_size_eval 8 \
	--minibatch_size=50 \
	--minibatch_eval=200 \
    --warmup_epochs 0 \
    --lr_floor_stop_training \
    --lr_floor 1e-8 \
    --lr_cooldown=0 \
	--learning_rate=2e-3 \
    --lr_scheduler='ReduceLROnPlateau' \
    --lr_scheduler_factor=0.6 \
    --lr_scheduler_patience=2 \
	--optimizer="Adam" \
	--epochs=100 \
    --early_stop \
    --early_stop_patience=5 \
	--checkpoint \
    --test_on_train