import requests
import json 
import os 
import time
from src.setup import SetupManager

class SpotifyManager:
    def __init__(self):
        if os.path.exists('spotify_auth.json'):
            with open('spotify_auth.json', 'r') as f:
                saved_data = json.load(f)
            if time.time() > saved_data['expires']:
                os.remove('spotify_auth.json')
            else:
                self.authorization = saved_data['authorization']
                self.client_token = saved_data['client_token']
                self.persisted_queries = saved_data['persisted_queries']
                self.library = saved_data['library']
                self.extra_headers = saved_data.get('extra_headers', {})
                self.session = None

        if not os.path.exists('spotify_auth.json'):
            self.session = SetupManager()
            self.client_token, self.authorization = self.session.get_library()
            self.library = self.session.library
            if not self.session.has_p_keys:
                self.session.get_persist_queries()
            self.persisted_queries = self.session.persisted_qs
            self.extra_headers = self.session.extra_headers
            with open('spotify_auth.json', 'w') as f:
                json.dump({
                   'client_token'       :   self.client_token,
                   'authorization'      :   self.authorization,
                   'library'            :   self.library,
                   'persisted_queries'  :   self.persisted_queries,
                   'extra_headers'      :   self.extra_headers,
                   'expires'            :   time.time() + 60*60
                },f)
        
        requests.get("http://localhost:5001/initialized")

    def _try_refresh_tokens(self):
        """Re-extract Spotify auth tokens from the browser without rebuilding library data."""
        try:
            if self.session is None:
                self.session = SetupManager()
            # Use _get_library_auth directly — it only captures tokens, doesn't touch library lists
            result = self.session._get_library_auth()
            if result is None:
                raise Exception("_get_library_auth returned None")
            client_token, authorization, _persisted = result
            self.client_token = client_token
            self.authorization = authorization
            # extra_headers are updated inside _extract_auth_from_network_logs when it finds a match
            self.extra_headers = self.session.extra_headers
            with open('spotify_auth.json', 'w') as f:
                json.dump({
                    'client_token':      self.client_token,
                    'authorization':     self.authorization,
                    'library':           self.library,
                    'persisted_queries': self.persisted_queries,
                    'extra_headers':     self.extra_headers,
                    'expires':           time.time() + 60 * 60
                }, f)
            print("Spotify tokens refreshed successfully.")
            return True
        except Exception as e:
            print(f"Failed to refresh Spotify tokens: {e}")
            return False

    def _get_res_from_spot(self, operation, persisted, uri=None, limit=50, _retried=False):
        variables = {
            "locale": "",
            "offset": 0,
            "limit": limit,
        }
        if uri:
            variables["uri"] = uri
        if operation == "fetchPlaylist":
            variables["enableWatchFeedEntrypoint"] = False
        endpoint = 'https://api-partner.spotify.com/pathfinder/v2/query'
        body = {
            'operationName': operation,
            'variables': variables,
            'extensions': json.loads(persisted) if isinstance(persisted, str) else persisted,
        }
        headers = {
            'accept': 'application/json',
            'authorization': self.authorization,
            'client-token': self.client_token,
            'content-type': 'application/json;charset=UTF-8',
            **self.extra_headers,
        }
        response = requests.post(endpoint, headers=headers, json=body)
        if response.status_code == 200:
            res_j = json.loads(response.text)
            return res_j, True
        if response.status_code == 401 and not _retried:
            print(f"Got 401 for {operation} — attempting token refresh...")
            if self._try_refresh_tokens():
                return self._get_res_from_spot(operation, persisted, uri, limit, _retried=True)
        print(f"Error in _get_res_from_spot ({operation}): {response.status_code}")
        return response.status_code, False
    
    @staticmethod
    def _extract_from_trackv2(tracks):
        extracted = []
        for track in tracks:
            track = track['track'] if 'track' in track else track['data']
            if 'name' not in track or track['name'].strip() == '': continue
            artists = "".join([","+artist['profile']['name'] for artist in track['artists']['items']]) if 'artists' in track else ""
            extracted.append((track['name'],artists[1:]))
        return extracted

    def get_playlist(self, uri, limit=50):
        res_j, success = self._get_res_from_spot('fetchPlaylist', self.persisted_queries['Playlists'], uri, limit)
        if success:
            try:
                playlist_v2 = res_j['data']['playlistV2']
                if playlist_v2 is None:
                    print(f"Spotify returned null for playlistV2 (uri={uri}) — playlist may be unavailable or private.")
                    return [], False
                tracks = playlist_v2['content']
                total_count = int(tracks['totalCount'])
                if total_count > limit:
                    return self.get_playlist(uri, total_count + 50 - (total_count % 50))
                fixed_tracks = [track['itemV2'] for track in tracks['items']]
                extracted = self._extract_from_trackv2(fixed_tracks)
                return extracted, success
            except (TypeError, KeyError) as e:
                print(f"Error parsing playlist response (uri={uri}): {e}")
                return [], False
        return res_j, success

    def get_artists(self, uri):
        # currently only choosing the topTracks
        res_j, success = self._get_res_from_spot('queryArtistOverview', self.persisted_queries['Artists'], uri)
        if success:
            try:
                top_tracks = res_j['data']['artistUnion']['discography']['topTracks']
                if top_tracks is None:
                    return [], False
                extracted = self._extract_from_trackv2(top_tracks['items'])
                return extracted, success
            except (TypeError, KeyError) as e:
                print(f"Error parsing artist response (uri={uri}): {e}")
                return [], False
        return res_j, success

    def get_albums(self, uri, limit=50):
        res_j, success = self._get_res_from_spot('getAlbum', self.persisted_queries['Albums'], uri, limit)
        if success:
            try:
                tracks = res_j['data']['albumUnion']['tracksV2']
                if tracks is None:
                    print(f"Spotify returned null for albumUnion tracksV2 (uri={uri}).")
                    return [], False
                total_count = int(tracks['totalCount'])
                if total_count > limit:
                    return self.get_albums(uri, total_count + 50 - (total_count % 50))
                extracted = self._extract_from_trackv2(tracks['items'])
                return extracted, success
            except (TypeError, KeyError) as e:
                print(f"Error parsing album response (uri={uri}): {e}")
                return [], False
        return res_j, success

    def get_liked(self, limit=50):
        res_j, success = self._get_res_from_spot('fetchLibraryTracks', self.persisted_queries['LikedSongs'], limit=limit)
        if success:
            try:
                tracks = res_j['data']['me']['library']['tracks']
                if tracks is None:
                    return [], False
                total_count = int(tracks['totalCount'])
                if total_count > limit:
                    return self.get_liked(total_count + 50 - (total_count % 50))
                fixed_tracks = [track['track'] for track in tracks['items']]
                extracted = self._extract_from_trackv2(fixed_tracks)
                return extracted, success
            except (TypeError, KeyError) as e:
                print(f"Error parsing liked songs response: {e}")
                return [], False
        return res_j, success
