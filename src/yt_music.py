import os
import json 
import requests
import ytmusicapi
from thefuzz import process, fuzz
from src.setup import SetupManager

class YT_Music:
    def __init__(self):
        if not os.path.exists('yt_headers.json'):
            self.session = SetupManager()
            self.cookies = self.session.yt_cookies
            with open('yt_headers.json', 'w') as f:
                json.dump(self.cookies,f)

        try:
            self.yt_sess = ytmusicapi.YTMusic("yt_headers.json")
        except Exception as e:
            print(e)
        requests.get("http://localhost:5001/update_login?status=true")
        self.filter_list = {}

    def _safe_search(self, q, limit, filter):
        """Wraps ytmusicapi.search with error handling for malformed results (e.g. musicCardShelfRenderer)."""
        try:
            return self.yt_sess.search(q, limit=limit, filter=filter if filter != '' else None)
        except Exception:
            # ytmusicapi crashed on musicCardShelfRenderer — retry with 'songs' filter
            # which never returns card-shelf results
            for f in ('songs', 'videos'):
                try:
                    return self.yt_sess.search(q, limit=limit, filter=f)
                except Exception:
                    continue
            return []

    def _parse_results(self, search_results, max_results=3):
        """Extract up to max_results song/video entries, skipping malformed ones."""
        search_dict = {}
        search_arr = []
        for result in search_results:
            try:
                res_type = result.get('resultType', '')
                if res_type not in ('video', 'song'):
                    continue
                artists = result.get('artists') or []
                title = result.get('title', '')
                vid_id = result.get('videoId', '')
                if not title or not vid_id:
                    continue
                searchable_text = title + ", " + ", ".join(a['name'] for a in artists)
                search_dict[searchable_text] = (vid_id, artists, title)
                search_arr.append(searchable_text)
                if len(search_arr) >= max_results:
                    break
            except Exception:
                continue
        return search_dict, search_arr

    def search_one(self, q, search_from_limit=25, filter='songs'):
        search_results = self._safe_search(q, search_from_limit, filter)
        search_dict, search_arr = self._parse_results(search_results)

        if len(search_arr) == 0:
            if len(q.split(',')) > 1:
                q = ", ".join(q.split(',')[:-1])
                return self.search_one(q, search_from_limit, filter=filter)
            elif filter != '':
                return self.search_one(q, search_from_limit, filter='')
            else:
                raise Exception(f"No results found for query: {q}")

        choice, confidence = process.extractOne(q, search_arr)
        if confidence < 85 and filter != '':
            return self.search_one(q, search_from_limit, filter='')
        vid_id, artists, title = search_dict[choice]
        return (title, ", ".join(a['name'] for a in artists), confidence, vid_id)

    def search_one_except(self, q, filter_str, search_from_limit=25, retries=0, filter='songs'):
        search_results = self._safe_search(q, search_from_limit, filter)
        if q not in self.filter_list:
            self.filter_list[q] = {filter_str}
        else:
            self.filter_list[q].add(filter_str)
        search_dict, search_arr = self._parse_results(search_results)
        # remove filtered-out results
        search_arr = [s for s in search_arr if s not in self.filter_list[q]]
        search_dict = {k: v for k, v in search_dict.items() if k in search_arr}

        if len(search_arr) == 0:
            if retries > 2:
                self.filter_list[q] = set()
            return self.search_one_except(q, filter_str, search_from_limit + 25, retries + 1)
        choice, confidence = process.extractOne(q, search_arr)
        if confidence < 85 and filter != '':
            return self.search_one_except(q, filter_str, search_from_limit, filter='')
        vid_id, artists, title = search_dict[choice]
        return (title, ", ".join(a['name'] for a in artists), confidence, vid_id)

    def search(self,q,limit=5,search_from_limit=25):
        search_results = self.yt_sess.search(q,limit=search_from_limit,ignore_spelling=True)
        search_dict = {}
        search_arr = []
        for result in search_results:
            cat = result['category']
            if  cat == "Top result" or cat == "Songs" or cat == "Videos":
                search_dict[result['title']] = result['videoId']
                search_arr.append(result['title'])
        choices = process.extract(q,search_arr,limit=limit,scorer=fuzz.token_sort_ratio)
        choices = [tuple(list(choice) + [search_dict[choice[0]]]) for choice in choices]
        return choices
        
    def get_library_playlists_cached(self):
        if not hasattr(self, '_yt_playlists_cache'):
            try:
                self._yt_playlists_cache = self.yt_sess.get_library_playlists(limit=None)
            except Exception as e:
                print(f"Error fetching YTM playlists: {e}")
                self._yt_playlists_cache = []
        return self._yt_playlists_cache

    def get_existing_playlist(self, name):
        """Returns (playlist_id, tracks_list) if playlist with this name exists on YTM,
        (None, []) if not found, or raises RuntimeError if tracks can't be fetched
        (so the caller can skip rather than blindly re-adding everything)."""
        playlists = self.get_library_playlists_cached()
        for pl in playlists:
            if pl.get('title', '').lower().strip() == name.lower().strip():
                pl_id = pl['playlistId']
                # Try with a large finite limit first; fall back to 5000 on failure.
                for limit in (None, 5000):
                    try:
                        full = self.yt_sess.get_playlist(pl_id, limit=limit)
                        return pl_id, full.get('tracks', [])
                    except Exception as e:
                        print(f"Error fetching YTM playlist '{name}' (limit={limit}): {e}")
                # Both attempts failed — raise so the caller can skip safely
                raise RuntimeError(
                    f"не удалось получить треки существующего плейлиста «{name}» на YouTube Music"
                )
        return None, []

    def find_missing_tracks(self, spotify_tracks, yt_tracks):
        """
        spotify_tracks: list of (title, artist) tuples from Spotify
        yt_tracks: list of track dicts from ytmusicapi
        Returns subset of spotify_tracks not found in yt_tracks (fuzzy match).
        """
        yt_strings = []
        for t in yt_tracks:
            try:
                title = t.get('title', '')
                artists = t.get('artists') or []
                artist_str = ', '.join(a['name'] for a in artists)
                yt_strings.append(f"{title} {artist_str}".lower())
            except Exception:
                continue

        if not yt_strings:
            return spotify_tracks  # nothing on YTM — all missing

        missing = []
        for sp_title, sp_artist in spotify_tracks:
            sp_str = f"{sp_title} {sp_artist}".lower()
            _, score = process.extractOne(sp_str, yt_strings)
            if score < 80:
                missing.append((sp_title, sp_artist))
        return missing

    def add_multiple_to_playlist(self,playlist_id,songs):
        if isinstance(songs[-1],tuple):
            songs = [songs[-1] for song in songs]
        status = self.yt_sess.add_playlist_items(playlist_id,songs)
        if status['status'] == 'STATUS_FAILED':
            # if duplicates, then prolly duplicates in user's library too, so just add it aswell
            status = self.yt_sess.add_playlist_items(playlist_id,songs,duplicates=True)
            if status['status'] == 'STATUS_FAILED':
                return False
        return True
    
    def create_playlist(self,name,desc):
        pl_id =  self.yt_sess.create_playlist(name, desc)
        if pl_id: return pl_id

    def create_and_add(self,playlist_name,desc,songs):
        playlist_id = self.create_playlist(playlist_name,desc)
        return self.add_multiple_to_playlist(playlist_id,songs)

