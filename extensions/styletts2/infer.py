import yaml
import torch
import random
import librosa
import torchaudio
import phonemizer
import numpy as np
from munch import *
from nltk.tokenize import word_tokenize
from extensions.styletts2.utils import recursive_munch
from extensions.styletts2.text_utils import TextCleaner
from extensions.styletts2.Utils.PLBERT.util import load_plbert
from extensions.styletts2.models import build_model, load_F0_models, load_ASR_models
from extensions.styletts2.Modules.diffusion.sampler import DiffusionSampler,ADPM2Sampler,KarrasSchedule


torch.manual_seed(0)
torch.backends.cudnn.benchmark = False
torch.backends.cudnn.deterministic = True
textclenaer = TextCleaner()
np.random.seed(0)
random.seed(0)
to_mel = torchaudio.transforms.MelSpectrogram(
    n_mels=80, n_fft=2048, win_length=1200, hop_length=300
)
mean, std = -4, 4


def length_to_mask(lengths):
    mask = (
        torch.arange(lengths.max())
        .unsqueeze(0)
        .expand(lengths.shape[0], -1)
        .type_as(lengths)
    )
    mask = torch.gt(mask + 1, lengths.unsqueeze(1))
    return mask


def preprocess(wave):
    wave_tensor = torch.from_numpy(wave).float()
    mel_tensor = to_mel(wave_tensor)
    mel_tensor = (torch.log(1e-5 + mel_tensor.unsqueeze(0)) - mean) / std
    return mel_tensor


def compute_style(path):
    wave, sr = librosa.load(path, sr=24000)
    audio, index = librosa.effects.trim(wave, top_db=30)
    if sr != 24000:
        audio = librosa.resample(audio, sr, 24000)
    mel_tensor = preprocess(audio).to(device)

    with torch.no_grad():
        ref_s = model.style_encoder(mel_tensor.unsqueeze(1))
        ref_p = model.predictor_encoder(mel_tensor.unsqueeze(1))

    return torch.cat([ref_s, ref_p], dim=1)


device = "cuda" if torch.cuda.is_available() else "cpu"


global_phonemizer = phonemizer.backend.EspeakBackend(
    language="en-us", preserve_punctuation=True, with_stress=True
)

config = yaml.safe_load(
    open("/home/eleven/tgenfork/text-generation-webui/extensions/styletts2/models/config_ft.yml")
)  # config here


# load pretrained ASR model
ASR_config = config.get("ASR_config", False)
ASR_path = config.get("ASR_path", False)
text_aligner = load_ASR_models(ASR_path, ASR_config)


# load pretrained F0 model
F0_path = config.get("F0_path", False)
pitch_extractor = load_F0_models(F0_path)


# load BERT model


BERT_path = config.get("PLBERT_dir", False)
plbert = load_plbert(BERT_path)


model_params = recursive_munch(config["model_params"])
model = build_model(model_params, text_aligner, pitch_extractor, plbert)
_ = [model[key].eval() for key in model]
_ = [model[key].to(device) for key in model]


params_whole = torch.load(
    "/home/eleven/tgenfork/text-generation-webui/extensions/styletts2/models/epoch_2nd_00230.pth",
    map_location="cpu",
)  # Model here
params = params_whole["net"]

for key in model:
    if key in params:
        print("%s loaded" % key)
        try:
            model[key].load_state_dict(params[key])
        except:
            from collections import OrderedDict

            state_dict = params[key]
            new_state_dict = OrderedDict()
            for k, v in state_dict.items():
                name = k[7:]  # remove `module.`
                new_state_dict[name] = v
            # load params
            model[key].load_state_dict(new_state_dict, strict=False)
#             except:
#                 _load(params[key], model[key])
_ = [model[key].eval() for key in model]


sampler = DiffusionSampler(
    model.diffusion.diffusion,
    sampler=ADPM2Sampler(),
    sigma_schedule=KarrasSchedule(
        sigma_min=0.0001, sigma_max=3.0, rho=9.0
    ),  # empirical parameters
    clamp=False,
)


def inference(text, ref_s, alpha=0.3, beta=0.7, diffusion_steps=5, embedding_scale=1):
    text = text.strip()
    ps = global_phonemizer.phonemize([text])
    ps = word_tokenize(ps[0])
    ps = " ".join(ps)
    tokens = textclenaer(ps)
    tokens.insert(0, 0)
    tokens = torch.LongTensor(tokens).to(device).unsqueeze(0)

    with torch.no_grad():
        input_lengths = torch.LongTensor([tokens.shape[-1]]).to(device)
        text_mask = length_to_mask(input_lengths).to(device)

        t_en = model.text_encoder(tokens, input_lengths, text_mask)
        bert_dur = model.bert(tokens, attention_mask=(~text_mask).int())
        d_en = model.bert_encoder(bert_dur).transpose(-1, -2)

        s_pred = sampler(
            noise=torch.randn((1, 256)).unsqueeze(1).to(device),
            embedding=bert_dur,
            embedding_scale=embedding_scale,
            features=ref_s,  # reference from the same speaker as the embedding
            num_steps=diffusion_steps,
        ).squeeze(1)

        s = s_pred[:, 128:]
        ref = s_pred[:, :128]

        ref = alpha * ref + (1 - alpha) * ref_s[:, :128]
        s = beta * s + (1 - beta) * ref_s[:, 128:]

        d = model.predictor.text_encoder(d_en, s, input_lengths, text_mask)

        x, _ = model.predictor.lstm(d)
        duration = model.predictor.duration_proj(x)

        duration = torch.sigmoid(duration).sum(axis=-1)
        pred_dur = torch.round(duration.squeeze()).clamp(min=1)

        pred_aln_trg = torch.zeros(input_lengths, int(pred_dur.sum().data))
        c_frame = 0
        for i in range(pred_aln_trg.size(0)):
            pred_aln_trg[i, c_frame : c_frame + int(pred_dur[i].data)] = 1
            c_frame += int(pred_dur[i].data)

        # encode prosody
        en = d.transpose(-1, -2) @ pred_aln_trg.unsqueeze(0).to(device)
        if model_params.decoder.type == "hifigan":
            asr_new = torch.zeros_like(en)
            asr_new[:, :, 0] = en[:, :, 0]
            asr_new[:, :, 1:] = en[:, :, 0:-1]
            en = asr_new

        F0_pred, N_pred = model.predictor.F0Ntrain(en, s)

        asr = t_en @ pred_aln_trg.unsqueeze(0).to(device)
        if model_params.decoder.type == "hifigan":
            asr_new = torch.zeros_like(asr)
            asr_new[:, :, 0] = asr[:, :, 0]
            asr_new[:, :, 1:] = asr[:, :, 0:-1]
            asr = asr_new

        out = model.decoder(asr, F0_pred, N_pred, ref.squeeze().unsqueeze(0))

    return out.squeeze().cpu().numpy()[..., :-50]

'''
def randomname():
    text = """ StyleTTS 2 is a text to speech model that leverages style diffusion and adversarial training with large speech language """
    reference_dicts = {}
    reference_dicts["1"] = "/home/eleven/StyleTTS2API/gpt-pilot/workspace/StyleAPI/StyleTTS2/text-generation-webui/extensions/styletts2/refaudio/audio0004.wav"

    noise = torch.randn(1, 1, 256).to(device)
    for k, path in reference_dicts.items():
        ref_s = compute_style(path)
        start = time.time()
        wav = inference(
            text, ref_s, alpha=0.3, beta=0.7, diffusion_steps=10, embedding_scale=1
        )
        rtf = (time.time() - start) / (len(wav) / 24000)
        print(f"RTF = {rtf:5f}")
        output_path = f"/home/eleven/StyleTTS2API/gpt-pilot/workspace/StyleAPI/StyleTTS2/text-generation-webui/extensions/styletts2/output/{k}_synthesized.wav"
        sf.write(output_path, wav, 24000)
        print(f"{k} Synthesized audio saved to: {output_path}")
'''