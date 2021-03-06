import shutil
import os
import youtube_dl
import paho.mqtt.client as mqtt
import threading
import requests
import re
import time
import logging
import logging.handlers
import yaml
import coloredlogs
import traceback
import json
import shlex
import spotipy
import mutagen
import mutagen.oggopus
import mutagen.easymp4
import mutagen.id3
import spotipy
import spotipy.util
import datetime


from queue import Queue
from mpd import MPDClient

destination_song = ""

class ydl_logger(object):
    os.makedirs("logs", exist_ok=True)
    log = logging.getLogger("YoutubeDL")
    handler = logging.handlers.RotatingFileHandler("logs/youtubedl.log", maxBytes=1000, backupCount=3)
    handler.setLevel(logging.INFO)
    formatter = logging.Formatter("%(asctime)s - %(name)s - %(levelname)s - %(message)s")
    handler.setFormatter(formatter)
    log.addHandler(handler)

    def error(self, msg):
        self.log.error(msg)

    def debug(self, msg):
        global destination_song # fuck you for forcing me to use this ytdl
        self.log.info(msg)

        if msg.startswith("Destination: "):
            destination_song  = "Destination: ".join(msg.split("Destination: ")[:1])

        elif msg.startswith("[ffmpeg] Post-process file "):
            destination_song  = msg.split("Post-process file ")[1].split(" exists")[0]

        elif msg.startswith("[ffmpeg] Destination: "):
            destination_song  = msg.split("[ffmpeg] Destination: ")[1]

class tube():
    log = logging.getLogger("MPDTube")
    config = {}
    songs = []
    spotify_token = None

    def __init__(self):
        self.setup_logging()
        self.log.info("MPDTube is starting")
        self.load_config()
        os.makedirs(self.config['paths']['download'], exist_ok=True)

        self.mqtt = mqtt.Client()
        self.mqtt.connect(self.config['mqtt']['host'], self.config['mqtt']['port'], 60)
        self.mqtt.on_connect = self.on_mqtt_connect
        self.mqtt.on_message = self.on_mqtt_message
        self.queue = Queue()

        self.ydl_opts = {'noprogress': True, 'format': 'bestaudio/best', "noplaylist": True, "default_search": "ytsearch",
                         'outtmpl': os.path.join(self.config['paths']['download'], "%(title)s.%(ext)s"), "no_color": True,
                         'postprocessors': [{'key': 'FFmpegExtractAudio',}], 'logger': ydl_logger(), }

        threading.Thread(target=self.queue_thread).start()
        self.login_spotify()
        self.mqtt.loop_forever()

    def setup_logging(self):
        os.makedirs("logs", exist_ok=True)
        handler = logging.handlers.RotatingFileHandler("logs/mpdtube.log", maxBytes=1000, backupCount=3)
        handler.setLevel(logging.INFO)
        formatter = logging.Formatter("%(asctime)s - %(name)s - %(levelname)s - %(message)s")
        handler.setFormatter(formatter)

        self.log.addHandler(handler)

    def load_config(self):
        with open("settings.yaml", "r") as f:
            self.config = yaml.safe_load(f)

    def login_spotify(self):
        if os.path.exists(".cache-%s" % format(self.config['spotify']['username'])) or "TERM" in os.environ:
            self.spotify_token = spotipy.util.prompt_for_user_token(self.config['spotify']['username'], "user-library-read",
                                                    self.config['spotify']['client']['id'],
                                                    self.config['spotify']['client']['secret'],
                                                    redirect_uri='http://localhost/mpdtube')
        else:
            self.log_warning("""No cache file with a Spotify token exists,
            and we are not running in a terminal so we can not request a token.
            \n\nSpotify support will be disabled!\n\n
            To get around this, run MPDTube in a terminal and follow the on-screen instructions.""")

    def queue_thread(self):
        self.log.info("Queue thread is up")
        while True:
            item = self.queue.get()
            if item:
                try:
                    self.play_song(item[0], item[1])
                except Exception as e:
                    self.log_error("An exception occured while processing %s (%s)" % (item, e))
                    self.log.error(traceback.print_exc())
                    if item[1]:
                        self.log_nurdbot("An exception occured while processing %s (%s)" % (item, item[0]), directMessage=True)

        self.log.warning("Queue thread has stopped")

    def convert_bytes(self, num):
        if num is None:
            return "0"
        step_unit = 1000.0 #1024 bad the size

        for x in ['bytes', 'KB', 'MB', 'GB', 'TB']:
            if num < step_unit:
                return "%3.1f %s" % (num, x)
            num /= step_unit

    def convert_time(self, time):
        return str(datetime.timedelta(seconds=time))

    def log_nurdbot(self, type, msg, payload, directMessage=False):
        if "nurdbot" in self.config and "jsb-udp" in self.config['nurdbot'] and "python2" in self.config['nurdbot']:
            message = shlex.quote("<MPDTUBE:%s> %s" % (type, msg))

            destination = payload['user']

            if payload['channel'][0] == "#" and not directMessage:
                destination = payload['channel']

            cmd = "%s %s -p %s -m %s" % (self.config['nurdbot']['python2'], self.config['nurdbot']['jsb-udp'], shlex.quote(destination) , message)
            os.system(cmd)

    def log_info(self, msg):
        self.log.info(msg)
        self.mqtt.publish(os.path.join(self.config['mqtt']['topics']['status'], "info") , msg)

    def log_error(self, msg):
        self.log.error(msg)
        self.mqtt.publish(os.path.join(self.config['mqtt']['topics']['status'], "error"), msg)

    def log_warning(self, msg):
        self.log.error(msg)
        self.mqtt.publish(os.path.join(self.config['mqtt']['topics']['status'], "error"), msg)

    def on_mqtt_connect(self, client, userdata, flags, rc):
        self.log.info("MQTT connected.")
        subscribe_topics = [self.config['mqtt']['topics']['play']]

        if "nurdbot" in self.config and "jsb-udp" in self.config['nurdbot'] and "python2" in self.config['nurdbot']:
            subscribe_topics.append(os.path.join(self.config['mqtt']['topics']['play'], "nurdbot"))

        for subscribe in subscribe_topics:
            self.log.info("Subscribing to: %s", subscribe)
            self.mqtt.subscribe(subscribe)
            self.mqtt.publish(self.config['mqtt']['topics']['status'], "MPDTube running.")

    def on_mqtt_message(self, client, userdata, msg):
        if msg.topic == self.config['mqtt']['topics']['play']:
            self.queue.put(({"query": msg.payload.decode("utf-8")}, False))
            self.log_info("Received on play: %s" % msg.payload.decode("utf-8"))

        elif msg.topic == os.path.join(self.config['mqtt']['topics']['play'], "nurdbot"):
            payload = json.loads(msg.payload.decode("utf-8"))
            self.queue.put((payload, True))
            self.log_info("Received on play (nurdbot): %s" % msg.payload.decode("utf-8"))

    def ydl_download(self, url):
        """ Send the query over to youtube-dl and download the file"""
        global destination_song

        with youtube_dl.YoutubeDL(self.ydl_opts) as ydl:
            ydl.extract_info(url, download=True)
            self.log_info("Destination song: %s" % destination_song)
            return destination_song

    def ydl_get_info(self, query):
        with youtube_dl.YoutubeDL(self.ydl_opts) as ydl:
            return ydl.extract_info(query, download=False)

    def extract_artist_title(self, audio_file):
        """
            Attempts to extract the artist - title from the file name and returns it as a tuple,
            if it failed to do so it will return only the title. Tuple is (title, artist)
        """

        audio_file = os .path.basename(audio_file)
        audio_re = re.match(r'(.*?)\s-\s(.+)', "".join(os.path.splitext(audio_file)[:-1]))

        if audio_re:
            return (audio_re.groups()[1], audio_re.groups()[0])

        return ("".join(os.path.splitext(audio_file)[:-1]), )

    def add_metadata(self, audio_file, metadict):
        """
            Add any metadata you want to an audio file, as long as mutagen supports it and you add it to mutagen_ext_mapping.
        """
        self.log_info("Adding metadata to %s (%s)" % (audio_file, metadict))

        # Get the extention of the audio file
        audio_ext = os.path.splitext(audio_file)[-1].lower()[1:]
        mutagen_class = None

        # Support for MP4 and M4A files
        if audio_ext == "m4a" or audio_ext == "mp4":
            mutagen_class = mutagen.mp4.MP4(audio_file)
            if "title" in metadict: mutagen_class['\xa9nam']  = metadict['title']
            if "artist" in metadict: mutagen_class['\xa9ART'] = metadict['artist']
            if "comment" in metadict: mutagen_class['\xa9cmt'] = metadict['comment']

        # Support for ogg and opus
        elif audio_ext == "ogg" or audio_ext == "opus":
            mutagen_class = mutagen.oggopus.OggOpus(audio_file)
            if "title" in metadict: mutagen_class['title']  = metadict['title']
            if "artist" in metadict: mutagen_class['artist'] = metadict['artist']
            if "comment" in metadict: mutagen_class['comment'] = metadict['comment']

        # Support for mp3s
        elif audio_ext == "mp3":
            mutagen_class = mutagen.id3.ID3() # Not loading a song here, instead we will do this later
            if "title" in metadict: mutagen_class['TIT2']  = mutagen.id3.TIT2(encoding=3, text=metadict['title'])
            if "artist" in metadict: mutagen_class['artist'] = mutagen.id3.TPE1(encoding=3, text=metadict['artist'])
            if "comment" in metadict: mutagen_class['COMM'] = mutagen.id3.COMM(encoding=3, lang=u'eng', desc='comment', text=metadict['comment'])

        else:
            raise Exception("Unsuported file format for metadata (%s, %s)" % (audio_ext, audio_file))

        if mutagen_class != None:
            mutagen_class.save(audio_file)

    def find_prio(self, mpd):
        """
            Increase the priority of every song and returns the lowest priority
            that should be used for the next song. Causing it to be played after the
            previous high prio songs.
        """

        lowest_prio = 255

        for song in mpd.playlistinfo():
            if "prio" in song:
                if int(song['prio']) < lowest_prio:
                    lowest_prio = int(song['prio'])

        if lowest_prio > 1:
            return lowest_prio - 1

        return 1

    def find_song_spotify(self, url):
        """ Try to return a spotify song into an youtube version of the song """

        if "spotify" in self.config:
            spotify = spotipy.Spotify(auth=self.spotify_token)

            try:
                track = spotify.track(url)
            except Exception as e:
                self.log_warning("Failed to process spotify url (%s)" % (url))
                return None

            return "%s - %s" % (track['artists'][0]['name'], track['name'])

        self.log_info("Spotify support not enabled.")
        return None


    def play_song(self, payload, nurdbot=False):
        duration = 0
        query = payload['query']

        if query.startswith("spotify:"):
           # Handle spotify urls
           query = self.find_song_spotify(query)

           if not query:
                self.log.warning("Failed to find anything on spotify for %s" % (query) )
                if nurdbot:
                    self.log_nurdbot("WARNING", "Failed to find anything on spotify for %s" % (query), payload, directMessage=True)

        if nurdbot:
            self.log_nurdbot("INFO", "Processing: %s" % (query), payload, directMessage=True)

        try:
            ydl_info = self.ydl_get_info(query)
        except:
            ydl_info = None
            self.log.warning("Failed to find anything for %s" % (query) )
            if nurdbot:
                self.log_nurdbot("WARNING", "Failed to find anything for %s" % (query), payload, directMessage=False)
            return

        # Pick the first entry if there are multiple items
        if "entries" in ydl_info:
            duration = ydl_info['entries'][0]['duration']
            filesize = ydl_info['entries'][0]['filesize']
            name = ydl_info['entries'][0]['title']
        else:
            duration = ydl_info['duration']
            name = ydl_info['title']

            # Not everything has a filesize e.g Soundcloud
            filesize = 0
            if "filesize" in ydl_info:
                filesize = ydl_info['filesize']

        if nurdbot:
            self.log_nurdbot("INFO", "Downloading %s (Duration: %s, size: %s)" % (name, self.convert_time(duration),
                                                                                  self.convert_bytes(filesize)), payload,
                                                                                  directMessage=True)

        #if nurdbot:
            #self.log_nurdbot("INFO", "Downloading ")
        # if "abuse" in self.config and "length" in self.config['abuse'] and duration > self.config['abuse']['length']:
        #     self.log.warning("Result for `%s` exceeds max length (%s > %s)" % (query, duration, self.config['abuse']['length']) )
        #     if nurdbot:
        #         self.log_nurdbot("WARNING", "Result for `%s` exceeds max length (%s > %s)" % (query, duration, self.config['abuse']['length']))
        #     return

        # if "abuse" in self.config and "filesize" in self.config['abuse'] and filesize > self.config['abuse']['filesize']:
        #     self.log.warning("Result for `%s` exceeds max length (%s > %s)" % (query, duration, self.config['abuse']['filesize']) )
        #     if nurdbot:
        #         self.log_nurdbot("WARNING", "Result for `%s` exceeds max file size (%s > %s)" % (query, filesize, self.config['abuse']['filesize']))
        #     return

        # Download the file
        file = self.ydl_download(query)
        if not file:
            return

        # Connect to MPD
        mpd = MPDClient()
        mpd.connect(self.config['mpd']['host'], self.config['mpd']['port'])

        if not os.path.exists(file):
            self.log_error("Couldn't find %s" % file)
            if nurdbot:
                self.log_nurdbot("ERROR", "Couldn't find '%s'" % file, payload)

            mpd.close()
            mpd.disconnect()
            return

        file = os.path.basename(file)

        self.log_info("Attempting to add: %s" % query)
        mpd.update(os.path.join(self.config['paths']['relative'], file))

        if not os.path.exists(file) and self.config['paths']['nfs_timeout'] > 0:
            # Wait a bit for it to sync back to NFS
            self.log_info("Waiting for NFS sync (Timeout in: %s seconds)" % self.config['paths']['nfs_timeout'])
            time_waited = 0

            while True:
                if os.path.exists(not os.path.exists(file)):
                    self.log.info("%s alraedy exists!", (file))
                    break

                if time_waited >= self.config['paths']['nfs_timeout']:
                    self.log.error("Timeout waiting for '%s' to appear! (NFS)", (file))
                    self.log_nurdbot("Timeout waiting for '%s' to appear! (NFS)", (file, payload))
                    return

                time.sleep(1)
                time_waited += 1


        # Set creation and modification time to now
        try:
            os.utime(os.path.join(self.config['paths']['download'], file), times=None)
        except Exception as e:
            self.log.error("Failed to set time for %s (%s)" % (file, e))

        # Set metadata
        title_artist = self.extract_artist_title(file)
        metadata = {"title": title_artist[0], "comment": "%s (requested by %s at %s)" % (query, payload['user'], time.time())}

        if len(title_artist) == 2:
            metadata.update({"artist": title_artist[1]})

        try:
            self.add_metadata(os.path.join(self.config['paths']['download'], file), metadata)
        except Exception as e:
            self.log.error("Mutagen error: %s" % (e))

        # Add song to MPD
        song_id = mpd.addid(os.path.join(self.config['paths']['relative'], file))

        if nurdbot:
            self.log_nurdbot("INFO", "Adding '%s' to MPD (pos: %s)" % (file, song_id), payload, directMessage=False)

        self.log_info("[%s] id is: '%s'" % (file, song_id))

        mpd_status = mpd.status()
        self.log_info("MPD status: %s" % mpd_status['state'])
        self.log.info("Current song is: %s" % mpd.currentsong()['file'])

        if mpd_status['state'] != "play":
            # Start playing the song right away if we aren't playing anything yet.

            self.log_info("playing '%s' right away" % file)
            mpd.playid(song_id)
            return

        elif mpd_status['random'] != "1":
            self.log_warning("MPD is not in random mode, this is currently unsupported! Automatically switching to Random mode")
            mpd.random(1)

        prio = self.find_prio(mpd)
        self.log_info("Queueing '%s' at priority '%s'" % (file, prio))
        mpd.prioid(prio, song_id)

        mpd.close()
        mpd.disconnect()

if __name__ == "__main__":

    logging.basicConfig(level=logging.INFO)
    coloredlogs.install(level='INFO', fmt="%(asctime)s %(name)s %(levelname)s %(message)s")
    mpdtube = tube()