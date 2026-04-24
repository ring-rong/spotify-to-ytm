import json
import requests
import urllib
import time
import undetected_chromedriver as uc
from os import path
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.common.exceptions import TimeoutException, NoSuchElementException

class SetupManager:

    _instance = None

    def __new__(cls):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
            cls._instance._webdriver_running = False
        return cls._instance
    
    def __init__(self):
        if not self._webdriver_running:
            pwd = path.dirname(__file__)
            options = uc.ChromeOptions()
            options.add_argument('--lang=en-US')
            options.add_experimental_option('prefs', {'intl.accept_languages': 'en,en-US'})
            options.set_capability('goog:loggingPrefs', {'performance': 'ALL'})
            self.driver = uc.Chrome(user_data_dir= f"{pwd}{path.sep}webdriver_profile2", options=options, version_main=147)
            self._webdriver_running = True

            self.extra_headers = {
                'app-platform': 'WebPlayer',
                'spotify-app-version': '1.2.89.282.g2b043556',
                'origin': 'https://open.spotify.com',
                'referer': 'https://open.spotify.com/',
                'user-agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/147.0.0.0 Safari/537.36',
            }
            self._login_spotify()
            self.library = {
                'Albums' : [],
                'Artists' : [],
                'Folders' : [],
                'HasLikedSongs' : False,
                'Playlists' : [],
                'TrashItems' : 0
            }

            self.has_p_keys = False

            self.persisted_qs = {
                'Albums' : '',
                'Artists' : '',
                'LikedSongs' : '',
                'Playlists' : '',
            }
            self.yt_cookies = None

        if not self.yt_cookies:
            self._get_ytm_cookies()

    def __exit__(self):
        if self._webdriver_running:
            self.driver.quit()
            self._webdriver_running = False
        return self

    def _login_spotify(self):
        # opening the homepage
        self.driver.get('https://open.spotify.com')
        
        #checking if alraedy logged in
        logged_in = True
        try:
            # we wait for the login button
            WebDriverWait(self.driver, 5).until(
                EC.presence_of_element_located((By.CSS_SELECTOR, '[data-testid="login-button"]'))
            )
            logged_in = False
        except TimeoutException:
            logged_in = True
        if not logged_in:
            user_confirm = False
            requests.get("http://localhost:5001/update_login?status=false&type=spotify")
            while not user_confirm:
                time.sleep(1)
                res = requests.get("http://localhost:5001/check_user_confirmation")
                user_confirm = "true" in res.text
        return logged_in
    
    @staticmethod
    def _extract_auth(url,headers):
        try:
            auth = headers['authorization']
            c_token = headers['client-token']
            temp_url = urllib.parse.unquote_plus(url)
            url_j = json.loads('{"id": ' + temp_url.split('&variables=')[1].replace('&extensions=', ',"extensions":') + '}')
            persisted =  json.dumps(url_j['extensions'])
            return c_token, auth, persisted
        except KeyError:
            temp_url = urllib.parse.unquote_plus(url)
            url_j = json.loads('{"id": ' + temp_url.split('&variables=')[1].replace('&extensions=', ',"extensions":') + '}')
            persisted =  json.dumps(url_j['extensions'])
            return None, None, json.dumps(url_j['extensions'])

    @staticmethod
    def _extract_auth_from_body(body, headers, operation_name=None):
        try:
            body_json = json.loads(body)
            # handle GraphQL batch requests (array of operations)
            if isinstance(body_json, list):
                if operation_name:
                    body_json = next((op for op in body_json if op.get('operationName') == operation_name), None)
                else:
                    body_json = body_json[0]
                if body_json is None:
                    raise KeyError(f"Operation '{operation_name}' not found in batch request")
            auth = headers['authorization']
            c_token = headers['client-token']
            persisted_query = json.dumps(body_json['extensions'])
            return c_token, auth, persisted_query
        except KeyError:
            raise KeyError("Could not extract authorization or client token from the request body.")
        

    def _extract_auth_from_network_logs(self, operation_name):
        logs = self.driver.get_log("performance")
        for log in logs:
            message = json.loads(log["message"])["message"]
            if message["method"] == "Network.requestWillBeSent":
                request = message["params"]["request"]
                url = request["url"]
                body = request.get("postData", "")
                if f"operationName={operation_name}" in url:
                    headers = request["headers"]
                    client_token, authorization, persisted_query = self._extract_auth(url,headers)
                    if client_token and authorization and persisted_query:
                        return client_token, authorization, persisted_query
                if f'"operationName":"{operation_name}"' in body:
                    headers = request["headers"]
                    client_token, authorization, persisted_query = self._extract_auth_from_body(body, headers, operation_name)
                    if client_token and authorization and persisted_query:
                        # capture extra headers Spotify requires
                        self.extra_headers = {
                            'app-platform': headers.get('app-platform', 'WebPlayer'),
                            'spotify-app-version': headers.get('spotify-app-version', ''),
                            'origin': 'https://open.spotify.com',
                            'referer': 'https://open.spotify.com/',
                            'user-agent': headers.get('user-agent', ''),
                        }
                        return client_token, authorization, persisted_query
        print(f"Could not find the {operation_name} request in the network logs.")
        if operation_name == "libraryV3":
            raise Exception("Could not fetch the library data. "\
                            "Make sure you are logged in to Spotify"\
                            "and the language is set to English.")
            
        return None

    
    def _get_library_auth(self):
        # turning on network logs
        self.driver.execute_cdp_cmd("Network.enable", {})
        # navigate to collection page — triggers libraryV3 request
        self.driver.get('https://open.spotify.com/collection/playlists')
        try:
            # wait for the library button to confirm page loaded
            WebDriverWait(self.driver, 10).until(
                EC.presence_of_element_located((By.CSS_SELECTOR, '[aria-label="Open Your Library"]'))
            )
            # click it to expand the library panel and trigger libraryV3
            library_button = self.driver.find_element(By.CSS_SELECTOR, '[aria-label="Open Your Library"]')
            library_button.click()
            time.sleep(3)
        except (TimeoutException, NoSuchElementException):
            print("Library button not found, trying to extract auth from navigation request...")
        return self._extract_auth_from_network_logs('libraryV3')


    def get_library(self):
        client_token, authorization, persisted_query = self._get_library_auth()

        url = 'https://api-partner.spotify.com/pathfinder/v2/query'
        limit = 50

        headers = {
            'accept': 'application/json',
            'authorization': authorization,
            'client-token': client_token,
            'content-type': 'application/json;charset=UTF-8',
            **self.extra_headers,
        }

        def make_body(limit):
            return {
                'operationName': 'libraryV3',
                'variables': {
                    'filters': [], 'order': None, 'textFilter': '',
                    'features': ['LIKED_SONGS', 'YOUR_EPISODES'],
                    'limit': limit, 'offset': 0, 'flatten': False,
                    'expandedFolders': [], 'folderUri': None,
                    'includeFoldersWhenFlattening': True
                },
                'extensions': json.loads(persisted_query)
            }

        response = requests.post(url, headers=headers, json=make_body(limit))
        if response.status_code != 200:
            print(f"Error! Code: {response.status_code}, Body: {response.text[:300]}")

        if response.status_code == 200:
            res_j = json.loads(response.text)
            total_count = res_j['data']['me']['libraryV3']['totalCount']
            if total_count > limit:
                response = requests.post(url, headers=headers, json=make_body((2 + total_count//25)*25))
                res_j = json.loads(response.text)
            
            for cnt, item in enumerate(res_j['data']['me']['libraryV3']['items']):
                data = item['item']['data']
                try:
                    if data['__typename'] == 'PseudoPlaylist':
                        self.library['HasLikedSongs'] = True
                        continue

                    if data['__typename'] == 'NotFound':
                        self.library['TrashItems'] += 1
                        continue
                    
                    if data['__typename'] == 'Folder':
                        self.library['Folders'].append({
                            'name' : data['name'],
                            'uri' :  data['uri'],
                        })
                        continue

                    if data['__typename'] == 'Artist':
                        self.library['Artists'].append({
                            'name' : data['profile']['name'],
                            'uri' :  data['uri'],
                            # TODO: 'thumb'
                        })
                        continue
                    
                    if data['__typename'] == 'Album':
                        self.library['Albums'].append({
                            'name' : data['name'],
                            'uri' :  data['uri'],
                            # TODO: 'thumb'
                        })
                        continue 

                    if data['__typename'] == 'Playlist':
                        self.library['Playlists'].append({
                            'name' : data['name'],
                            'uri' :  data['uri'],
                            # TODO: 'thumb'
                        })
                        continue
                except KeyError:
                    raise KeyError(f"KeyError: {data['__typename']}\nMore info: {item}")
                print(f"Unsuported type: {data['__typename']}\nMore info: {item}")

        return client_token, authorization

    def _get_persisted_liked(self):
        self.driver.get('https://open.spotify.com/collection/tracks')
        self.driver.implicitly_wait(3)

        try:
            WebDriverWait(self.driver, 7).until(
                EC.presence_of_element_located((By.CSS_SELECTOR, '[data-testid="play-button"]'))
            )
        except TimeoutException:
            print("woopsies")

        # again
        self.driver.get('https://open.spotify.com/collection/tracks')
        self.driver.implicitly_wait(3)

        try:
            WebDriverWait(self.driver, 7).until(
                EC.presence_of_element_located((By.CSS_SELECTOR, '[data-testid="play-button"]'))
            )
        except TimeoutException:
            print("woopsies")


        client_token, authorization, persisted_query = self._extract_auth_from_network_logs('fetchLibraryTracks')
        if client_token and authorization and persisted_query:
            self.persisted_qs['LikedSongs'] = persisted_query
            return True
        return False

    def _get_persisted_playlists(self):
        self.driver.get('https://open.spotify.com/playlist/3QqoFD4Y4XaLoQBYkh2cAj')
        self.driver.implicitly_wait(3)

        try:
            WebDriverWait(self.driver, 7).until(
                EC.presence_of_element_located((By.CSS_SELECTOR, '[data-testid="play-button"]'))
            )
        except TimeoutException:
            print("woopsies")

        client_token, authorization, persisted_query = \
            self._extract_auth_from_network_logs('fetchPlaylist') or \
            self._extract_auth_from_network_logs('fetchPlaylistWithGatedEntityRelations')
        if client_token and authorization and persisted_query:
            self.persisted_qs['Playlists'] = persisted_query
            return True
        
        return False

    def _get_persisted_albums(self):
        self.driver.get('https://open.spotify.com/album/19WTqbdqDMWMthZfkmxSbx')
        self.driver.implicitly_wait(3)
        
        try:
            WebDriverWait(self.driver, 7).until(
                EC.presence_of_element_located((By.CSS_SELECTOR, '[data-testid="play-button"]'))
            )
        except TimeoutException:
            print("woopsies")

        client_token, authorization, persisted_query = self._extract_auth_from_network_logs('getAlbum')
        if client_token and authorization and persisted_query:
            self.persisted_qs['Albums'] = persisted_query
            return True
        return False        

    def _get_persisted_artists(self):
        self.driver.get('https://open.spotify.com/artist/483Rl4WY6iIJ9czOrOgymb')
        self.driver.implicitly_wait(3)

        try:
            WebDriverWait(self.driver, 7).until(
                EC.presence_of_element_located((By.CSS_SELECTOR, '[data-testid="play-button"]'))
            )
        except TimeoutException:
            print("woopsies")

        client_token, authorization, persisted_query = self._extract_auth_from_network_logs('queryArtistOverview')
        if client_token and authorization and persisted_query:
            self.persisted_qs['Artists'] = persisted_query
            return True
        return False

    def get_persist_queries(self):
        if not self.has_p_keys:
            if self.library['Albums']:
                self._get_persisted_albums()
            if self.library['Artists']:
                self._get_persisted_artists()
            if self.library['Playlists']:
                self._get_persisted_playlists()
            if self.library['HasLikedSongs']:
                self._get_persisted_liked()
            self.has_p_keys = True

    def _login_ytm(self):
        self.driver.get("https://music.youtube.com/")

        # check if already logged in via avatar button
        logged_in = False
        try:
            WebDriverWait(self.driver, 10).until(
                EC.presence_of_element_located((By.CSS_SELECTOR, '#avatar-btn, ytmusic-settings-button'))
            )
            logged_in = True
        except TimeoutException:
            # not logged in — check that the page loaded at all
            try:
                WebDriverWait(self.driver, 5).until(
                    EC.presence_of_element_located((By.CSS_SELECTOR, 'ytmusic-app, #header'))
                )
            except TimeoutException:
                raise Exception("Network Error: YouTube Music failed to load")
        if not logged_in:
            user_confirm = False
            requests.get("http://localhost:5001/update_login?status=false&type=ytm")
            while not user_confirm:
                time.sleep(1)
                res = requests.get("http://localhost:5001/check_user_confirmation")
                user_confirm = "true" in res.text
        requests.get("http://localhost:5001/update_login?status=true")
        return logged_in
    
    def _get_cookies(self):
        return self.driver.get_cookies()

    def _get_ytm_cookies(self):
        self._login_ytm()
        # turning on network logs again just to be sure
        self.driver.execute_cdp_cmd("Network.enable", {})
        # the driver might be already on the home page but going back to ytm just in case
        self.driver.get('https://music.youtube.com/')

        WebDriverWait(self.driver, 10).until(
            EC.presence_of_element_located((By.CSS_SELECTOR, '#avatar-btn, ytmusic-settings-button'))
        )

        last_height = self.driver.execute_script("return document.body.scrollHeight")

        while True:
            self.driver.execute_script("window.scrollTo(0, document.body.scrollHeight*0.90);")

            time.sleep(0.5)

            new_height = self.driver.execute_script("return document.body.scrollHeight")
            if new_height == last_height:
                break
            last_height = new_height

        logs = self.driver.get_log("performance")

        for log in logs:
            message = json.loads(log["message"])["message"]
            if message["method"] == "Network.requestWillBeSent":
                request = message["params"]["request"]
                url = request["url"]
                # `browse?` is what we need for the cookies
                if "browse?" in url:
                    self.yt_cookies = request["headers"]
                    cookies_j = self._get_cookies()
                    extracted_c = ""
                    for cookie in cookies_j:
                        c = f"{cookie['name']}={cookie['value']}; "
                        extracted_c += c
                    self.yt_cookies['cookie'] = extracted_c[:-2]
                    return True