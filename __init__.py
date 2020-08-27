# Copyright 2017 Mycroft AI Inc.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#    http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import re
from mycroft.skills.core import intent_handler
from mycroft.util.parse import match_one, fuzzy_match
from mycroft.api import DeviceApi
from mycroft.messagebus import Message
from requests import HTTPError
from adapt.intent import IntentBuilder

import time
from os.path import abspath, dirname, join
from subprocess import call, Popen, DEVNULL
import signal
from socket import gethostname

import random

from mycroft.skills.common_play_skill import CommonPlaySkill, CPSMatchLevel

from enum import Enum

from ytmusicapi import YTMusic

class YoutubePlaybackError(Exception):
    pass


class PlaylistNotFoundError(Exception):
    pass


class YoutubeNotAuthorizedError(Exception):
    pass


# Return value definition indication nothing was found
# (confidence None, data None)
NOTHING_FOUND = (None, 0.0)

# Confidence levels for generic play handling
DIRECT_RESPONSE_CONFIDENCE = 0.8

MATCH_CONFIDENCE = 0.5


def best_result(results):
    """Return best result from a list of result tuples.

    Arguments:
        results (list): list of spotify result tuples

    Returns:
        Best match in list
    """
    return results[0]

def best_confidence(title, query):
    """Find best match for a title against a query.

    Some titles include ( Remastered 2016 ) and similar info. This method
    will test the raw title and a version that has been parsed to remove
    such information.

    Arguments:
        title: title name from search response
        query: query from user

    Returns:
        (float) best condidence
    """
    best = title.lower()
    best_stripped = re.sub(r'(\(.+\)|-.+)$', '', best).strip()
    return max(fuzzy_match(best, query),
               fuzzy_match(best_stripped, query))


def status_info(status):
    """Return track, artist, album tuple from spotify status.

    Arguments:
        status (dict): Spotify status info

    Returns:
        tuple (track, artist, album)
     """
    return ('', '', '')


class YoutubeMusicSkill(CommonPlaySkill):
    """Youtube Music."""

    def __init__(self):
        super(YoutubeMusicSkill, self).__init__()
        self.yt = YTMusic()
        self.idle_count = 0
        self.ducking = False
        self.mouth_text = None

        self._playlists = None
        self.saved_tracks = None
        self.regexes = {}
        self.last_played_type = None  # The last uri type that was started
        self.is_playing = False

    def translate_regex(self, regex):
        if regex not in self.regexes:
            path = self.find_resource(regex + '.regex')
            if path:
                with open(path) as f:
                    string = f.read().strip()
                self.regexes[regex] = string
        return self.regexes[regex]

    def initialize(self):
        super().initialize()
        # Setup handlers for playback control messages
        self.add_event('mycroft.audio.service.next', self.next_track)
        self.add_event('mycroft.audio.service.prev', self.prev_track)
        self.add_event('mycroft.audio.service.pause', self.pause)
        self.add_event('mycroft.audio.service.resume', self.resume)
        # Check and then monitor for credential changes
        self.settings_change_callback = self.on_websettings_changed
        self.on_websettings_changed()

    def on_websettings_changed(self):
        # Only attempt to load credentials if the username has been set
        # will limit the accesses to the api.
        self.load_credentials()

    def load_credentials(self):
        """Retrieve credentials from the backend and connect to Youtube."""
        pass

    def failed_auth(self):
        if 'user' not in self.settings:
            self.log.error('Settings hasn\'t been received yet')
            self.speak_dialog('NoSettingsReceived')
        elif not self.settings.get("user"):
            self.log.error('User info has not been set.')
            # Assume this is initial setup
            self.speak_dialog('NotConfigured')
        else:
            # Assume password changed or there is a typo
            self.log.error('User info has been set but Auth failed.')
            self.speak_dialog('NotAuthorized')

    ######################################################################
    # Handle auto ducking when listener is started.

    def handle_listener_started(self, message):
        """Handle auto ducking when listener is started.

        The ducking is enabled/disabled using the skill settings on home.

        TODO: Evaluate the Idle check logic
        """
        if (self.is_playing and self.settings.get('use_ducking', False)):
            self.__pause()
            self.ducking = True

            # Start idle check
            self.idle_count = 0
            self.cancel_scheduled_event('IdleCheck')
            self.schedule_repeating_event(self.check_for_idle, None,
                                          1, name='IdleCheck')

    def check_for_idle(self):
        """Repeating event checking for end of auto ducking."""
        if not self.ducking:
            self.cancel_scheduled_event('IdleCheck')
            return

        active = self.enclosure.display_manager.get_active()
        if not active == '' or active == 'YoutubeMusicSkill':
            # No activity, start to fall asleep
            self.idle_count += 1

            if self.idle_count >= 5:
                # Resume playback after 5 seconds of being idle
                self.cancel_scheduled_event('IdleCheck')
                self.ducking = False
                self.resume()
        else:
            self.idle_count = 0

    ######################################################################
    # Mycroft display handling

    def start_monitor(self):
        """Monitoring and current song display."""
        # Clear any existing event
        self.stop_monitor()

        # Schedule a new one every 5 seconds to monitor/update display
        self.schedule_repeating_event(self._update_display,
                                      None, 5,
                                      name='MonitorYoutube')
        self.add_event('recognizer_loop:record_begin',
                       self.handle_listener_started)

    def stop_monitor(self):
        # Clear any existing event
        self.cancel_scheduled_event('MonitorYoutube')

    def _update_display(self, message):
        # Checks once a second for feedback
        pass

    def CPS_match_query_phrase(self, phrase):
        """Handler for common play framework Query."""

        youtube_specified = 'youtube' in phrase
        bonus = 0.1 if youtube_specified else 0.0
        phrase = re.sub(self.translate_regex('on_youtube'), '', phrase)

        confidence, data = self.continue_playback(phrase, bonus)
        if not data:
            confidence, data = self.specific_query(phrase, bonus)
            if not data:
                confidence, data = self.generic_query(phrase, bonus)

        if data:
            self.log.info('Youtube Music confidence: {}'.format(confidence))
            self.log.info('                    data: {}'.format(data))

            if data.get('type') in ['saved_tracks', 'album', 'artist',
                                    'track', 'playlist', 'show']:
                if youtube_specified:
                    # " play great song on youtube'
                    level = CPSMatchLevel.EXACT
                else:
                    if confidence > 0.9:
                        # TODO: After 19.02 scoring change
                        # level = CPSMatchLevel.MULTI_KEY
                        level = CPSMatchLevel.TITLE
                    elif confidence < 0.5:
                        level = CPSMatchLevel.GENERIC
                    else:
                        level = CPSMatchLevel.TITLE
                    phrase += ' on youtube'
            elif data.get('type') == 'continue':
                if youtube_specified > 0:
                    # "resume playback on youtube"
                    level = CPSMatchLevel.EXACT
                else:
                    # "resume playback"
                    level = CPSMatchLevel.GENERIC
                    phrase += ' on youtube'
            else:
                self.log.warning('Unexpected youtube type: '
                                 '{}'.format(data.get('type')))
                level = CPSMatchLevel.GENERIC

            return phrase, level, data
        else:
            self.log.debug('Couldn\'t find anything to play on Youtube')

    def continue_playback(self, phrase, bonus):
        if phrase.strip() == 'youtube':
            return (1.0,
                    {
                        'data': None,
                        'name': None,
                        'type': 'continue'
                    })
        else:
            return NOTHING_FOUND

    def specific_query(self, phrase, bonus):
        """
        Check if the phrase can be matched against a specific youtube request.

        This includes asking for saved items, playlists, albums, podcasts,
        artists or songs.

        Arguments:
            phrase (str): Text to match against
            bonus (float): Any existing match bonus

        Returns: Tuple with confidence and data or NOTHING_FOUND
        """
        # Check if playlist
        #match = re.match(self.translate_regex('playlist'), phrase)
        #if match:
        #    return self.query_playlist(match.groupdict()['playlist'])

        # Check album
        match = re.match(self.translate_regex('album'), phrase)
        if match:
            bonus += 0.1
            album = match.groupdict()['album']
            return self.query_album(album, bonus)

        # Check artist
        match = re.match(self.translate_regex('artist'), phrase)
        if match:
            artist = match.groupdict()['artist']
            return self.query_artist(artist, bonus)
        match = re.match(self.translate_regex('song'), phrase)
        if match:
            song = match.groupdict()['track']
            return self.query_song(song, bonus)

        # Check if podcast
        #match = re.match(self.translate_regex('podcast'), phrase)
        #if match
        #    return self.query_show(match.groupdict()['podcast'])

        return NOTHING_FOUND

    def generic_query(self, phrase, bonus):
        """Check for a generic query, not asking for any special feature.

        This will try to parse the entire phrase in the following order
        - As a user playlist
        - As an album
        - As a track
        - As a public playlist

        Arguments:
            phrase (str): Text to match against
            bonus (float): Any existing match bonus

        Returns: Tuple with confidence and data or NOTHING_FOUND
        """
        self.log.info('Handling "{}" as a genric query...'.format(phrase))
        results = []

        # Check for artist
        self.log.info('Checking artists')
        conf, data = self.query_artist(phrase, bonus)
        if conf and conf > DIRECT_RESPONSE_CONFIDENCE:
            return conf, data
        elif conf and conf > MATCH_CONFIDENCE:
            results.append((conf, data))

        # Check for track
        self.log.info('Checking tracks')
        conf, data = self.query_song(phrase, bonus)
        if conf and conf > DIRECT_RESPONSE_CONFIDENCE:
            return conf, data
        elif conf and conf > MATCH_CONFIDENCE:
            results.append((conf, data))

        # Check for album
        self.log.info('Checking albums')
        conf, data = self.query_album(phrase, bonus)
        if conf and conf > DIRECT_RESPONSE_CONFIDENCE:
            return conf, data
        elif conf and conf > MATCH_CONFIDENCE:
            results.append((conf, data))

        # Check for public playlist
        self.log.info('Checking tracks')
        conf, data = self.get_best_public_playlist(phrase)
        if conf and conf > DIRECT_RESPONSE_CONFIDENCE:
            return conf, data
        elif conf and conf > MATCH_CONFIDENCE:
            results.append((conf, data))

        return best_result(results)

    def query_artist(self, artist, bonus=0.0):
        """Try to find an artist.

        Arguments:
            artist (str): Artist to search for
            bonus (float): Any bonus to apply to the confidence

        Returns: Tuple with confidence and data or NOTHING_FOUND
        """
        bonus += 0.1
        data = self.yt.search(artist, 'artists')
        if data:
            from pprint import pprint
            pprint(data[0])
            best = data[0]['artist']
            confidence = fuzzy_match(best, artist.lower()) + bonus
            confidence = min(confidence, 1.0)
            return (confidence,
                    {
                        'browseId': data[0]['browseId'],
                        'name': None,
                        'type': 'artist'
                    })
        else:
            return NOTHING_FOUND

    def query_album(self, album, bonus):
        """Try to find an album.

        Searches Youtube Music  by album and artist if available.

        Arguments:
            album (str): Album to search for
            bonus (float): Any bonus to apply to the confidence

        Returns: Tuple with confidence and data or NOTHING_FOUND
        """
        self.log.info('CHECKING FOR ALBUMS!')
        data = None
        by_word = ' {} '.format(self.translate('by'))
        if len(album.split(by_word)) > 1:
            album, artist = album.split(by_word)
            album_search = '*{}* artist:{}'.format(album, artist)
            bonus += 0.1
        else:
            album_search = album

        res = self.yt.search(album_search, 'albums')
        if res:
            self.log.info(res)
            return (0.7, {'type': 'album',
                          'browseId': res[0]['browseId']})
        else:
            return NOTHING_FOUND

    def query_playlist(self, playlist):
        """Try to find a playlist.

        First searches the users playlists, then tries to find a public
        one.

        Arguments:
            playlist (str): Playlist to search for

        Returns: Tuple with confidence and data or NOTHING_FOUND
        """
        result, conf = self.get_best_user_playlist(playlist)
        if playlist and conf > 0.5:
            uri = self.playlists[result]
            return (conf, {'data': uri,
                           'name': playlist,
                           'type': 'playlist'})
        else:
            return self.get_best_public_playlist(playlist)

    def query_song(self, song, bonus):
        """Try to find a song.

        Searches Youtube Music for song and artist if provided.

        Arguments:
            song (str): Song to search for
            bonus (float): Any bonus to apply to the confidence

        Returns: Tuple with confidence and data or NOTHING_FOUND
        """
        data = None
        by_word = ' {} '.format(self.translate('by'))
        if len(song.split(by_word)) > 1:
            song, artist = song.split(by_word)
            song_search = '*{}* artist:{}'.format(song, artist)
        else:
            song_search = song

        # Get song from youtube
        res = self.yt.search(song_search, 'songs')

        return None

    def CPS_start(self, phrase, data):
        """Handler for common play framework start playback request."""
        try:
            if data['type'] == 'continue':
                self.acknowledge()
                self.continue_current_playlist(dev)
            elif data['type'] == 'playlist':
                self.play_playlist(data['playlistId'])
            elif data['type'] == 'artist':
                self.play_artist(data['browseId'])
            elif data['type'] == 'album':
                self.play_album(data['browseId'])
            self.enable_playing_intents()
            if data.get('type') and data['type'] != 'continue':
                self.last_played_type = data['type']
            self.is_playing = True

        except PlaylistNotFoundError:
            self.speak_dialog('PlaybackFailed',
                              {'reason': self.translate('PlaylistNotFound')})
        except Exception as e:
            self.log.exception(str(e))
            self.speak_dialog('PlaybackFailed', {'reason': str(e)})

    def play_playlist(self, playlist_id):
        self.log.info('Playlist playback not implemented!!!')

    def play_album(self, browse_id):
        album = self.yt.get_album(browse_id)
        tracks = [t['videoId'] for t in album['tracks']]
        self.log.info('playing album!')
        self.log.info(tracks)

    def play_artist(self, browse_id):
        artist = self.yt.get_artist(browse_id)
        tracks = [t['videoId'] for t in artist['songs']['results']]
        from pprint import pprint
        pprint(tracks[0])

    def create_intents(self):
        """Setup the intents."""
        intent = IntentBuilder('').require('Youtube').require('Search') \
                                  .require('For')
        #self.register_intent(intent, self.search_youtube)
        self.register_intent_file('ShuffleOn.intent', self.shuffle_on)
        self.register_intent_file('ShuffleOff.intent', self.shuffle_off)
        self.register_intent_file('WhatSong.intent', self.song_info)
        self.register_intent_file('WhatAlbum.intent', self.album_info)
        self.register_intent_file('WhatArtist.intent', self.artist_info)
        self.register_intent_file('StopMusic.intent', self.handle_stop)
        time.sleep(0.5)
        self.disable_playing_intents()

    def enable_playing_intents(self):
        self.enable_intent('WhatSong.intent')
        self.enable_intent('WhatAlbum.intent')
        self.enable_intent('WhatArtist.intent')
        self.enable_intent('StopMusic.intent')

    def disable_playing_intents(self):
        self.disable_intent('WhatSong.intent')
        self.disable_intent('WhatAlbum.intent')
        self.disable_intent('WhatArtist.intent')
        self.disable_intent('StopMusic.intent')

    def shuffle_on(self):
        """ Turn on shuffling """
        pass

    def shuffle_off(self):
        """ Turn off shuffling """
        pass

    def song_info(self, message):
        """ Speak song info. """
        pass

    def album_info(self, message):
        """Speak album info."""
        pass

    def artist_info(self, message):
        """Speak artist info."""
        pass

    def __pause(self):
        self.audioservice.pause()

    def pause(self, message=None):
        """ Handler for playback control pause. """
        self.ducking = False
        self.__pause()

    def resume(self, message=None):
        """ Handler for playback control resume. """
        # if authorized and playback was started by the skill
        self.audioservice.resume()

    def next_track(self, message):
        """ Handler for playback control next. """
        self.audioservice.next_track()

    def prev_track(self, message):
        """Handler for playback control prev."""
        self.audioservice.prev_track()

    def handle_stop(self, message):
        self.bus.emit(Message('mycroft.stop'))

    def do_stop(self):
        self.pause(None)

    def stop(self):
        """ Stop playback. """
        if self.is_playing:
            self.do_stop()
            return True
        else:
            return False

    def shutdown(self):
        """ Remove the monitor at shutdown. """
        self.stop_monitor()


def create_skill():
    return YoutubeMusicSkill()
