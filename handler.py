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
    logger.info(f"Queueing prompt to: {url}")
    p = {"prompt": prompt, "client_id": client_id}
    data = json.dumps(p).encode('utf-8')
    
    # Log workflow content for debugging
    logger.info(f"Number of workflow nodes: {len(prompt)}")
    if input_type == "image":
        logger.info(f"Image node (284) setting: {prompt.get('284', {}).get('inputs', {}).get('image', 'NOT_FOUND')}")
    else:
        logger.info(f"Video node (228) setting: {prompt.get('228', {}).get('inputs', {}).get('video', 'NOT_FOUND')}")
    logger.info(f"Audio node (125) setting: {prompt.get('125', {}).get('inputs', {}).get('audio', 'NOT_FOUND')}")
    logger.info(f"Text node (241) setting: {prompt.get('241', {}).get('inputs', {}).get('positive_prompt', 'NOT_FOUND')}")
    if person_count == "multi":
        if "307" in prompt:
            logger.info(f"Second audio node (307) setting: {prompt.get('307', {}).get('inputs', {}).get('audio', 'NOT_FOUND')}")
        elif "313" in prompt:
            logger.info(f"Second audio node (313) setting: {prompt.get('313', {}).get('inputs', {}).get('audio', 'NOT_FOUND')}")
    
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
                # Read file directly using fullpath and encode to base64
                with open(video['fullpath'], 'rb') as f:
                    video_data = base64.b64encode(f.read()).decode('utf-8')
                videos_output.append(video_data)
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
    max_frames = int(max_duration * fps) + 81
    
    logger.info(f"Longest audio duration: {max_duration:.2f} seconds, calculated max_frames: {max_frames}")
    return max_frames

def handler(job):
    job_input = job.get("input", {})

    logger.info(f"Received job input: {job_input}")
    task_id = f"task_{uuid.uuid4()}"

    # Check input type and person count
    input_type = job_input.get("input_type", "image")  # "image" or "video"
    person_count = job_input.get("person_count", "single")  # "single" or "multi"
    
    logger.info(f"Workflow type: {input_type}, person count: {person_count}")

    # Determine workflow file path
    workflow_path = get_workflow_path(input_type, person_count)
    logger.info(f"Workflow to use: {workflow_path}")

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

    # Validate required fields and set default values
    prompt_text = job_input.get("prompt", "A person talking naturally")
    width = job_input.get("width", 512)
    height = job_input.get("height", 512)
    
    # Set max_frame (auto-calculate based on audio duration if not provided)
    max_frame = job_input.get("max_frame")
    if max_frame is None:
        logger.info("max_frame not provided. Auto-calculating based on audio duration.")
        max_frame = calculate_max_frames_from_audio(wav_path, wav_path_2 if person_count == "multi" else None)
    else:
        logger.info(f"User-specified max_frame: {max_frame}")
    
    logger.info(f"Workflow settings: prompt='{prompt_text}', width={width}, height={height}, max_frame={max_frame}")
    logger.info(f"Media path: {media_path}")
    logger.info(f"Audio path: {wav_path}")
    if person_count == "multi":
        logger.info(f"Second audio path: {wav_path_2}")

    prompt = load_workflow(workflow_path)

    # Check file existence
    if not os.path.exists(media_path):
        logger.error(f"Media file does not exist: {media_path}")
        return {"error": f"Media file not found: {media_path}"}
    
    if not os.path.exists(wav_path):
        logger.error(f"Audio file does not exist: {wav_path}")
        return {"error": f"Audio file not found: {wav_path}"}
    
    if person_count == "multi" and wav_path_2 and not os.path.exists(wav_path_2):
        logger.error(f"Second audio file does not exist: {wav_path_2}")
        return {"error": f"Second audio file not found: {wav_path_2}"}
    
    logger.info(f"Media file size: {os.path.getsize(media_path)} bytes")
    logger.info(f"Audio file size: {os.path.getsize(wav_path)} bytes")
    if person_count == "multi" and wav_path_2:
        logger.info(f"Second audio file size: {os.path.getsize(wav_path_2)} bytes")

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
    logger.info(f"Connecting to WebSocket: {ws_url}")
    
    # First check if HTTP connection is possible
    http_url = f"http://{server_address}:8188/"
    logger.info(f"Checking HTTP connection to: {http_url}")
    
    # Check HTTP connection (max 3 minutes)
    max_http_attempts = 180
    for http_attempt in range(max_http_attempts):
        try:
            import urllib.request
            response = urllib.request.urlopen(http_url, timeout=5)
            logger.info(f"HTTP connection successful (attempt {http_attempt+1})")
            break
        except Exception as e:
            logger.warning(f"HTTP connection failed (attempt {http_attempt+1}/{max_http_attempts}): {e}")
            if http_attempt == max_http_attempts - 1:
                raise Exception("Cannot connect to ComfyUI server. Please verify the server is running.")
            time.sleep(1)
    
    ws = websocket.WebSocket()
    # Attempt WebSocket connection (max 3 minutes)
    max_attempts = int(180/5)  # 3 minutes (attempt every 5 seconds)
    for attempt in range(max_attempts):
        import time
        try:
            ws.connect(ws_url)
            logger.info(f"WebSocket connection successful (attempt {attempt+1})")
            break
        except Exception as e:
            logger.warning(f"WebSocket connection failed (attempt {attempt+1}/{max_attempts}): {e}")
            if attempt == max_attempts - 1:
                raise Exception("WebSocket connection timeout (3 minutes)")
            time.sleep(5)
    videos = get_videos(ws, prompt, input_type, person_count)
    ws.close()

    # Handle case when video is not found
    for node_id in videos:
        if videos[node_id]:
            return {"video": videos[node_id][0]}
    
    return {"error": "Video not found"}

runpod.serverless.start({"handler": handler})