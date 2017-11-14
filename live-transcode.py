import glob
import os
import shutil
import subprocess
import time
from string import Template
from threading import Thread

from flask import Flask, request
from pymediainfo import MediaInfo
app = Flask(__name__)

DASHPATH = "/mnt/Data/Temp"
FFMPEG_PARAMS = "-copyts -avoid_negative_ts disabled -c:a aac -c:v libx264 -b:v 2000k -profile:v main -bf 1 -keyint_min 10 -g 10 -sc_threshold 0 -b_strategy 0 -min_seg_duration 10000000 -use_timeline 0 -f dash"

# Start Timestamp, Duration, Input filename, output MPD location
FFMPEG_TEMPLATE = "ffmpeg -ss %s -t %s -i \"%s\" " + FFMPEG_PARAMS + " %s"

mpd_template = Template(open("manifest.mpd.template").read())

os.chdir(DASHPATH)

chunk_dir = 0
chunk_info = {}
segment_duration = 0
current_video_filename = ''

# dumb config values that will need to be changed later
CHUNK_NUM = 1
MAX_CHUNK = 999  # XXX this should be calculated based on duration and segment duration

def fixerupper(chunk_offset, chunk_count, stage_dir, outdir):
	chunk_range = range(1, chunk_count+1)
	s0_chunks = list(chunk_range)
	s1_chunks = list(chunk_range)
	
	while s0_chunks or s1_chunks:
		files = os.listdir(stage_dir)
		if not files:
			time.sleep(0.1)
			continue
		file = files[0]
		
		if file.endswith('.tmp'):
			continue
		
		if "init" in file:
			if os.path.isfile("CHUNKS-init/%s" % file):
				os.remove("%s/%s" % (stage_dir, file))
			else:
				shutil.move("%s/%s" % (stage_dir, file), "CHUNKS-init/")
			continue
		
		if not "chunk" in file:
			os.remove("%s/%s" % (stage_dir, file))
			continue

		chunk_num = int(file.split('-')[-1].split('.')[0])
		
		if chunk_num not in chunk_range:
			os.remove("%s/%s" % (stage_dir, file))
			continue
		
		if 'stream0' in file:
			s0_chunks.remove(chunk_num)
		else:
			s1_chunks.remove(chunk_num)
		
		chunk_data = open("%s/%s" % (stage_dir, file)).read()
		chunk_data = list(chunk_data)
		#new_offset = tfdt * (chunk_num + chunk_offset - 2)
		# Fix TFDT.BaseMediaDecodeTime
		chunk_data[0x99] = chunk_data[0x31]
		chunk_data[0x9A] = chunk_data[0x32]
		#chunk_data[0x99] = chr(new_offset >> 8) # TFDT
		#chunk_data[0x9A] = chr(new_offset & 0xFF)
		#chunk_data[0x31] = chr(new_offset >> 8) # SIDX
		#chunk_data[0x32] = chr(new_offset & 0xFF)
		#chunk_data[0x63] = chr(chunk_num + chunk_offset - 1) # MFHD
		chunk_data = ''.join(chunk_data)
		fout = open("%s/%s" % (outdir, file), 'w')
		fout.write(chunk_data)
		fout.close()
		
		os.remove("%s/%s" % (stage_dir, file))

def CHUNKRUNNEREXTREME(first_chunk, last_chunk, chunk_dir):
	global segment_duration, current_video_filename

	start_time = "%s" % ((first_chunk-1) * segment_duration)
	duration = "%s" % ((last_chunk - first_chunk + 1) * segment_duration)
	input_file = current_video_filename
	
	outdir_base = "CHUNKS-%s" % chunk_dir
	outdir = outdir_base + "-staging"
	os.mkdir(outdir_base)
	os.mkdir(outdir)
	output_mpd = "%s/manifest.mpd" % outdir
	
	ffmpeg_command = FFMPEG_TEMPLATE % (start_time, duration, input_file, output_mpd)
	print "CHUNKRUNNER: ", ffmpeg_command
	
	# disable output
	ffmpeg_command = "%s >/dev/null 2>&1" % ffmpeg_command
	
	post_processor = Thread(target=fixerupper, args=(first_chunk, last_chunk - first_chunk + 1, outdir, outdir_base))
	post_processor.start()
	
	subprocess.call(ffmpeg_command, shell=True)
	print "CHUNKRUNNER: COMPLETE"

@app.route('/dash/manifest.mpd')
def manifest():
	global segment_duration, current_video_filename, mpd_template
	
	current_video_filename = request.args.get('fname')
	if not current_video_filename:
		print "no file set"
		return -1

	media_info = MediaInfo.parse(current_video_filename)
	general_info = media_info.tracks[0]
	
	uneven_fps = False
	
	if general_info.frame_rate == '25.000':
		frame_rate_rational = '25/1'
	elif general_info.frame_rate == '29.970':
		frame_rate_rational = '30000/1001'
		uneven_fps = True
	elif general_info.frame_rate == '23.976':
		frame_rate_rational = '24000/1001'
		uneven_fps = True
	else:
		print "OH SHIT", general_info.frame_rate
		return -1
	
	if uneven_fps:
		segment_duration = 10.01
	else:
		segment_duration = 10

	duration = general_info.duration
	total_seconds = duration / 1000.0
	minutes = int(total_seconds / 60)
	seconds = total_seconds % 60
	
	mpd_duration = "%sM%.1fS" % (minutes, seconds)
	mpd_segment_duration = int(segment_duration * 1e6)  # usec
	
	return mpd_template.substitute(total_duration=mpd_duration, 
								   frame_rate_rational=frame_rate_rational, 
								   segment_duration_usec=mpd_segment_duration)
	
@app.route('/dash/init-stream<int:stream_id>.m4s')
def init(stream_id):
	fname = "CHUNKS-init/init-stream%s.m4s" % stream_id

	if not os.path.isfile(fname):
		# Kick off transcoding to get initializer
		check_chunk(1)

	while not os.path.isfile(fname) or not os.path.getsize(fname):
		time.sleep(0.1)

	return open(fname).read()

def check_chunk(chunk_id):
	global chunk_dir, chunk_info

	# Check if requested chunk and the ones in the near future are in progress
	earliest_missing_chunk = None
	latest_missing_chunk = None
	chunk_max = min(MAX_CHUNK+1, chunk_id + CHUNK_NUM)
	for cid in range(chunk_id, chunk_max):
		if cid not in chunk_info:
			earliest_missing_chunk = cid
			break
	if earliest_missing_chunk:
		chunk_max = min(MAX_CHUNK+1, earliest_missing_chunk + CHUNK_NUM)
		for cid in range(earliest_missing_chunk, chunk_max):
			if cid in chunk_info:
				latest_missing_chunk = cid - 1
				break
	if earliest_missing_chunk and not latest_missing_chunk:
		latest_missing_chunk = cid
	
	# Spawn FFMPEG for the missing chunk range (if needed)
	if earliest_missing_chunk:
		print "Missing chunk range (inclusive): ", earliest_missing_chunk, latest_missing_chunk
		chunk_dir += 1
		internal_chunk_num = 1
		for cid in range(earliest_missing_chunk, latest_missing_chunk+1):
			chunk_info[cid] = {'dir': chunk_dir, 'num': internal_chunk_num}
			internal_chunk_num += 1
		chunk_runner = Thread(target=CHUNKRUNNEREXTREME, args=(earliest_missing_chunk, latest_missing_chunk, chunk_dir))
		chunk_runner.start()


@app.route('/dash/chunk-stream<int:stream_id>-<int:chunk_id>.m4s')
def chunk(stream_id, chunk_id):
	global chunk_dir, chunk_info
	
	check_chunk(chunk_id)
	
	# Ok. chunk info is registered and is either ready or in progress
	current_chunk_info = chunk_info[chunk_id]
	current_chunk_dir = current_chunk_info['dir']
	current_chunk_id = current_chunk_info['num']
	
	current_chunk_fname = "CHUNKS-%s/chunk-stream%s-%05d.m4s" % (current_chunk_dir, stream_id, current_chunk_id)
	
	# Wait until it's there on disk. ffmpeg flushes once for each chunk, so this should be okay for now.
	while not os.path.isfile(current_chunk_fname):
		print "Waiting for chunk: ", current_chunk_fname
		time.sleep(0.1)
	
	return open(current_chunk_fname).read()

# Clean up previous runs
for folder in glob.glob("CHUNKS*"):
	shutil.rmtree(folder)
os.mkdir("CHUNKS-init")

app.run('0.0.0.0', 8888)
