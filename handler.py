import runpod
from runpod.serverless.utils import rp_upload
import os
import websocket
import base64
import json
import uuid
import logging
import urllib.request
import urllib.parse
import binascii # Import for Base64 error handling
import subprocess
import time
import librosa
# Logging configuration
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


server_address = os.getenv('SERVER_ADDRESS', '127.0.0.1')
client_id = str(uuid.uuid4())

def download_file_from_url(url, output_path):
    """Function to download file from URL"""
    try:
        # Download file using wget
        result = subprocess.run([
            'wget', '-O', output_path, '--no-verbose', '--timeout=30', url
        ], capture_output=True, text=True, timeout=60)
        
        if result.returncode == 0:
            logger.info(f"✅ Successfully downloaded file from URL: {url} -> {output_path}")
            return output_path
        else:
            logger.error(f"❌ wget download failed: {result.stderr}")
            raise Exception(f"URL download failed: {result.stderr}")
    except subprocess.TimeoutExpired:
        logger.error("❌ Download timeout")
        raise Exception("Download timeout")
    except Exception as e:
        logger.error(f"❌ Error occurred during download: {e}")
        raise Exception(f"Error occurred during download: {e}")

def save_base64_to_file(base64_data, temp_dir, output_filename):
    """Function to save Base64 data to file"""
    try:
        # Decode Base64 string
        decoded_data = base64.b64decode(base64_data)
        
        # Create directory if it doesn't exist
        os.makedirs(temp_dir, exist_ok=True)
        
        # Save to file
        file_path = os.path.abspath(os.path.join(temp_dir, output_filename))
        with open(file_path, 'wb') as f:
            f.write(decoded_data)
        
        logger.info(f"✅ Saved Base64 input to file: '{file_path}'")
        return file_path
    except (binascii.Error, ValueError) as e:
        logger.error(f"❌ Base64 decoding failed: {e}")
        raise Exception(f"Base64 decoding failed: {e}")

def process_input(input_data, temp_dir, output_filename, input_type):
    """Function to process input data and return file path"""
    if input_type == "path":
        # Return path as-is if it's a path
        logger.info(f"📁 Processing path input: {input_data}")
        return input_data
    elif input_type == "url":
        # Download if it's a URL
        logger.info(f"🌐 Processing URL input: {input_data}")
        os.makedirs(temp_dir, exist_ok=True)
        file_path = os.path.abspath(os.path.join(temp_dir, output_filename))
        return download_file_from_url(input_data, file_path)
    elif input_type == "base64":
        # Decode and save if it's Base64
        logger.info(f"🔢 Processing Base64 input")
        return save_base64_to_file(input_data, temp_dir, output_filename)
    else:
        raise Exception(f"Unsupported input type: {input_type}")

def queue_prompt(prompt, input_type="image", person_count="single"):
    url = f"http://{server_address}:8188/prompt"
    p = {"prompt": prompt, "client_id": client_id}
    data = json.dumps(p).encode('utf-8')
    
    req = urllib.request.Request(url, data=data)
    req.add_header('Content-Type', 'application/json')
    
    try:
        response = urllib.request.urlopen(req)
        result = json.loads(response.read())
        logger.info(f"Prompt sent successfully: {result}")
        return result
    except urllib.error.HTTPError as e:
        logger.error(f"HTTP error occurred: {e.code} - {e.reason}")
        logger.error(f"Response content: {e.read().decode('utf-8')}")
        raise
    except Exception as e:
        logger.error(f"Error while sending prompt: {e}")
        raise

def get_image(filename, subfolder, folder_type):
    url = f"http://{server_address}:8188/view"
    logger.info(f"Getting image from: {url}")
    data = {"filename": filename, "subfolder": subfolder, "type": folder_type}
    url_values = urllib.parse.urlencode(data)
    with urllib.request.urlopen(f"{url}?{url_values}") as response:
        return response.read()

def get_history(prompt_id):
    url = f"http://{server_address}:8188/history/{prompt_id}"
    logger.info(f"Getting history from: {url}")
    with urllib.request.urlopen(url) as response:
        return json.loads(response.read())

def get_videos(ws, prompt, input_type="image", person_count="single"):
    prompt_id = queue_prompt(prompt, input_type, person_count)['prompt_id']
    output_videos = {}
    while True:
        out = ws.recv()
        if isinstance(out, str):
            message = json.loads(out)
            if message['type'] == 'executing':
                data = message['data']
                if data['node'] is None and data['prompt_id'] == prompt_id:
                    break
        else:
            continue

    history = get_history(prompt_id)[prompt_id]
    for node_id in history['outputs']:
        node_output = history['outputs'][node_id]
        videos_output = []
        if 'gifs' in node_output:
            for video in node_output['gifs']:
                # Upload to RunPod S3 and get full URL
                try:
                    # Try upload_file which returns full URL
                    video_url = rp_upload.upload_file(
                        file_path=video['fullpath'],
                        file_name=f"output_{uuid.uuid4()}.mp4"
                    )
                except Exception as e:
                    logger.warning(f"upload_file failed: {e}, trying upload_file_to_bucket")
                    # Fallback to upload_file_to_bucket
                    bucket_path = rp_upload.upload_file_to_bucket(
                        file_name=f"output_{uuid.uuid4()}.mp4",
                        file_location=video['fullpath']
                    )
                    # Construct full URL from bucket path
                    video_url = f"https://{os.getenv('RUNPOD_POD_ID', 'runpod')}.runpod.io/{bucket_path}"
                
                logger.info(f"Video uploaded: {video_url}")
                videos_output.append(video_url)
        output_videos[node_id] = videos_output

    return output_videos

def load_workflow(workflow_path):
    with open(workflow_path, 'r') as file:
        return json.load(file)

def get_workflow_path(input_type, person_count):
    """Return appropriate workflow file path based on input_type and person_count"""
    if input_type == "image":
        if person_count == "single":
            return "/I2V_single.json"
        else:  # multi
            return "/I2V_multi.json"
    else:  # video
        if person_count == "single":
            return "/V2V_single.json"
        else:  # multi
            return "/V2V_multi.json"

def get_audio_duration(audio_path):
    """Return audio file duration in seconds"""
    try:
        duration = librosa.get_duration(path=audio_path)
        return duration
    except Exception as e:
        logger.warning(f"Failed to calculate audio duration ({audio_path}): {e}")
        return None

def calculate_max_frames_from_audio(wav_path, wav_path_2=None, fps=25):
    """Calculate max_frames based on audio duration"""
    durations = []
    
    # Calculate first audio duration
    duration1 = get_audio_duration(wav_path)
    if duration1 is not None:
        durations.append(duration1)
        logger.info(f"First audio duration: {duration1:.2f} seconds")
    
    # Calculate second audio duration (for multi-person)
    if wav_path_2:
        duration2 = get_audio_duration(wav_path_2)
        if duration2 is not None:
            durations.append(duration2)
            logger.info(f"Second audio duration: {duration2:.2f} seconds")
    
    if not durations:
        logger.warning("Cannot calculate audio duration. Using default value 81.")
        return 81
    
    # Calculate max_frames based on longest audio duration
    max_duration = max(durations)
    max_frames = int(max_duration * fps) + 25  # Reduced padding from 81 to 25 for faster generation
    
    logger.info(f"Longest audio duration: {max_duration:.2f} seconds, calculated max_frames: {max_frames}")
    return max_frames

def handler(job):
    job_input = job.get("input", {})
    logger.info(f"Processing job: {job_input.get('input_type', 'image')}, {job_input.get('person_count', 'single')}")
    task_id = f"task_{uuid.uuid4()}"

    # Check input type and person count
    input_type = job_input.get("input_type", "image")  # "image" or "video"
    person_count = job_input.get("person_count", "single")  # "single" or "multi"

    # Determine workflow file path
    workflow_path = get_workflow_path(input_type, person_count)

    # Process image/video input
    media_path = None
    if input_type == "image":
        # Process image input (use only one of image_path, image_url, image_base64)
        if "image_path" in job_input:
            media_path = process_input(job_input["image_path"], task_id, "input_image.jpg", "path")
        elif "image_url" in job_input:
            media_path = process_input(job_input["image_url"], task_id, "input_image.jpg", "url")
        elif "image_base64" in job_input:
            media_path = process_input(job_input["image_base64"], task_id, "input_image.jpg", "base64")
        else:
            # Use default value
            media_path = "/examples/image.jpg"
            logger.info("Using default image file: /examples/image.jpg")
    else:  # video
        # Process video input (use only one of video_path, video_url, video_base64)
        if "video_path" in job_input:
            media_path = process_input(job_input["video_path"], task_id, "input_video.mp4", "path")
        elif "video_url" in job_input:
            media_path = process_input(job_input["video_url"], task_id, "input_video.mp4", "url")
        elif "video_base64" in job_input:
            media_path = process_input(job_input["video_base64"], task_id, "input_video.mp4", "base64")
        else:
            # Use default value (use default image if video is not provided)
            media_path = "/examples/image.jpg"
            logger.info("Using default image file: /examples/image.jpg")

    # Process audio input (use only one of wav_path, wav_url, wav_base64)
    wav_path = None
    wav_path_2 = None  # Second audio for multi-person
    
    if "wav_path" in job_input:
        wav_path = process_input(job_input["wav_path"], task_id, "input_audio.wav", "path")
    elif "wav_url" in job_input:
        wav_path = process_input(job_input["wav_url"], task_id, "input_audio.wav", "url")
    elif "wav_base64" in job_input:
        wav_path = process_input(job_input["wav_base64"], task_id, "input_audio.wav", "base64")
    else:
        # Use default value
        wav_path = "/examples/audio.mp3"
        logger.info("Using default audio file: /examples/audio.mp3")
    
    # Process second audio for multi-person
    if person_count == "multi":
        if "wav_path_2" in job_input:
            wav_path_2 = process_input(job_input["wav_path_2"], task_id, "input_audio_2.wav", "path")
        elif "wav_url_2" in job_input:
            wav_path_2 = process_input(job_input["wav_url_2"], task_id, "input_audio_2.wav", "url")
        elif "wav_base64_2" in job_input:
            wav_path_2 = process_input(job_input["wav_base64_2"], task_id, "input_audio_2.wav", "base64")
        else:
            # Use default value (same as first audio)
            wav_path_2 = wav_path
            logger.info("No second audio provided, using first audio")

    # Validate required fields and set default values (optimized for fast cartoon animations)
    prompt_text = job_input.get("prompt", "A person talking naturally")
    width = job_input.get("width", 854)  # 16:9 aspect ratio
    height = job_input.get("height", 480)  # 16:9 aspect ratio
    steps = job_input.get("steps", 4)  # 4 steps = 30-40% faster than 6
    
    # Set max_frame (auto-calculate based on audio duration if not provided)
    max_frame = job_input.get("max_frame")
    if max_frame is None:
        max_frame = calculate_max_frames_from_audio(wav_path, wav_path_2 if person_count == "multi" else None)
    
    logger.info(f"Settings: {width}x{height}, steps={steps}, max_frame={max_frame}")

    prompt = load_workflow(workflow_path)

    # Check file existence
    if not os.path.exists(media_path):
        return {"error": f"Media file not found: {media_path}"}
    if not os.path.exists(wav_path):
        return {"error": f"Audio file not found: {wav_path}"}
    if person_count == "multi" and wav_path_2 and not os.path.exists(wav_path_2):
        return {"error": f"Second audio file not found: {wav_path_2}"}

    # Configure workflow nodes
    if input_type == "image":
        # I2V workflow: configure image input
        prompt["284"]["inputs"]["image"] = media_path
    else:
        # V2V workflow: configure video input
        prompt["228"]["inputs"]["video"] = media_path
    
    # Common settings
    prompt["125"]["inputs"]["audio"] = wav_path
    prompt["241"]["inputs"]["positive_prompt"] = prompt_text
    prompt["245"]["inputs"]["value"] = width
    prompt["246"]["inputs"]["value"] = height
    prompt["270"]["inputs"]["value"] = max_frame
    
    # Set generation steps (node 128 is the sampler)
    if "128" in prompt:
        prompt["128"]["inputs"]["steps"] = steps
    
    # Configure second audio for multi-person
    if person_count == "multi":
        # Configure second audio node based on workflow type
        if input_type == "image":  # For I2V_multi.json
            if "307" in prompt:
                prompt["307"]["inputs"]["audio"] = wav_path_2
        else:  # For V2V_multi.json
            if "313" in prompt:
                prompt["313"]["inputs"]["audio"] = wav_path_2

    ws_url = f"ws://{server_address}:8188/ws?clientId={client_id}"
    http_url = f"http://{server_address}:8188/"
    
    # Check HTTP connection (max 30 seconds)
    max_http_attempts = 30
    for http_attempt in range(max_http_attempts):
        try:
            import urllib.request
            response = urllib.request.urlopen(http_url, timeout=5)
            logger.info(f"HTTP connection successful")
            break
        except Exception as e:
            if http_attempt == max_http_attempts - 1:
                raise Exception("Cannot connect to ComfyUI server. Please verify the server is running.")
            time.sleep(1)
    
    ws = websocket.WebSocket()
    # Attempt WebSocket connection (max 10 seconds)
    max_attempts = 10
    for attempt in range(max_attempts):
        import time
        try:
            ws.connect(ws_url)
            logger.info(f"WebSocket connected")
            break
        except Exception as e:
            if attempt == max_attempts - 1:
                raise Exception("WebSocket connection timeout")
            time.sleep(1)
    videos = get_videos(ws, prompt, input_type, person_count)
    ws.close()

    # Return video URL
    for node_id in videos:
        if videos[node_id]:
            logger.info(f"Video generation complete")
            return {"video_url": videos[node_id][0]}
    
    return {"error": "Video not found"}

runpod.serverless.start({"handler": handler})