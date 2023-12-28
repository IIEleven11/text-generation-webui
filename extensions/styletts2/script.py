
import time
import gradio as gr
import soundfile as sf
from pathlib import Path
from extensions.styletts2.infer import inference, compute_style


params = {
    "display_name": "StyleTTS2 Extension",
    "is_tab": False,
    "reference_audio_path": "/home/eleven/tgenfork/text-generation-webui/extensions/styletts2/refaudio/",  # Placeholder path for reference audio
    "activate_tts": False,
    "autoplay": False,
}


def setup():
    global ref_s
    audio_file_path = params["reference_audio_path"] + "/audio0004.wav"
    ref_s = compute_style(audio_file_path)


def styletts_speak(text):
    # Function to convert text to speech
    ref_s = compute_style(params["reference_audio_path"])
    wav = inference(
        text, ref_s, alpha=0.3, beta=0.7, diffusion_steps=10, embedding_scale=1
    )
    output_file = "/home/eleven/tgenfork/text-generation-webui/extensions/styletts2/outputs/styletts_output.wav"
    sf.write(str(output_file), wav, 24000)
    return output_file


def output_modifier(string, state):
    
    if params.get("activate_tts", False):
        ref_s = compute_style(params["reference_audio_path"])
        wav = inference(
            string, ref_s, alpha=0.3, beta=0.7, diffusion_steps=10, embedding_scale=1
        )
        output_file = Path("public/styletts_output.wav")
        sf.write(str(output_file), wav, 24000)
        autoplay = 'autoplay' if params.get("autoplay", False) else ''
        audio_html = f'<audio src="file/{output_file.as_posix()}" controls {autoplay}></audio>'
        return audio_html
    else:
        return string
    
    
def history_modifier(history):
    # Add autoplay to the last reply
    if len(history['internal']) > 0:
        history['visible'][-1] = [
            history['visible'][-1][0],
            history['visible'][-1][1].replace('controls>', 'controls autoplay>')
        ]

    return history
    
    
def get_available_voices():
    return sorted(
        [
            voice.name
            for voice in Path(
                "/home/eleven/tgenfork/text-generation-webui/extensions/styletts2/refaudio/"
            ).glob("*.wav")
        ]
    )


def voice_preview(string):
    # Function to convert text to speech using for voice preview
    ref_s = compute_style(params["reference_audio_path"])
    wav = inference(
        string, ref_s, alpha=0.3, beta=0.7, diffusion_steps=10, embedding_scale=1
    )
    output_file = Path("public/styletts_output.wav")
    sf.write(str(output_file), wav, 24000)
    return output_file


def ui():
    with gr.Blocks() as demo:
        with gr.Row():
            activate_tts = gr.Checkbox(
                label="Activate StyleTTS2", value=params["activate_tts"]
            )
            autoplay = gr.Checkbox(
                label="Play TTS automatically", value=params["autoplay"]
            )
            activate_tts.change(
                lambda x: params.update({"activate_tts": x}), activate_tts, None
            )
            autoplay.change(lambda x: params.update({"autoplay": x}), autoplay, None)
        with gr.Row():
            # dropdown for selecting the default voice
            default_voice = gr.Dropdown(
                label="Default Voice", choices=get_available_voices()
            )
            default_voice.change(
                lambda x: params.update(
                    {
                        "reference_audio_path": "/home/eleven/tgenfork/text-generation-webui/extensions/styletts2/refaudio/"
                        + (x if x is not None else "")
                    }
                ),
                default_voice,
                None,
            )

        with gr.Row():
            preview_text = gr.Text(
                show_label=False,
                placeholder="Preview1 text",
                elem_id="styletts_preview_text",
            )
            preview_play = gr.Button("Preview")
            preview_audio = gr.HTML(visible=False)

            preview_text.submit(voice_preview, preview_text, preview_audio)
            preview_play.click(voice_preview, preview_text, preview_audio)