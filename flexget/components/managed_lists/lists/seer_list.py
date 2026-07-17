"""Create a Seerr managed list."""

from __future__ import annotations

from collections.abc import MutableSet
from typing import TYPE_CHECKING, Any, Literal

from loguru import logger
from requests import HTTPError
from typing_extensions import TypedDict  # for Python <3.11 with (Not)Required

from flexget import plugin
from flexget.entry import Entry
from flexget.event import event
from flexget.utils import requests
from flexget.utils.requests import RequestException

if TYPE_CHECKING:
    from typing_extensions import NotRequired

log = logger.bind(name='seer_list')

SUPPORTED_IDS: list[Literal['tmdb_id', 'imdb_id', 'tvdb_id', 'seer_id']] = ['tmdb_id', 'imdb_id', 'tvdb_id', 'seer_id']

# Seerr status codes
STATUS_PENDING = 1
STATUS_APPROVED = 2
STATUS_AVAILABLE = 3
STATUS_DECLINING = 4
STATUS_DELETED = 5

STATUS_TEXT = {
    STATUS_PENDING: 'pending',
    STATUS_APPROVED: 'approved',
    STATUS_AVAILABLE: 'available',
    STATUS_DECLINING: 'declining',
    STATUS_DELETED: 'deleted',
}


class ApiError(Exception):
    """Exception raised when an API call fails."""

    def __init__(self, response: dict[str, Any]) -> None:
        self.response = response
        super().__init__(response.get('message', response.get('error', 'API Error')))


class Config(TypedDict):
    """The config schema for the Seerr managed list."""

    url: str
    api_key: NotRequired[str]
    email: NotRequired[str]
    password: NotRequired[str]
    type: Literal['shows', 'seasons', 'episodes', 'movies']
    status: Literal['approved', 'pending', 'available', 'all']
    hide_available: bool
    on_remove: NotRequired[Literal['pending', 'deleting', 'declined']]
    include_year: bool
    include_ep_title: bool


class SeerrRequest:
    """HTTP client for the Seerr API.

    Seerr uses cookie-based authentication (not Bearer tokens).
    - API key mode: uses X-Api-Key header
    - Cookie mode: POST /auth/local with {email, password}, then uses session cookie
    """

    def __init__(self, config: Config) -> None:
        self.base_url = config['url'].rstrip('/')
        self.config: Config = config
        self.cookie_handler = None
        self._auth_mode = None
        self._auth_headers = {}

        if 'api_key' in self.config:
            self._auth_mode = 'api_key'
            self._auth_headers = {'X-Api-Key': self.config['api_key']}
        elif self.config.get('email') and self.config.get('password'):
            self._auth_mode = 'cookie'
            self._authenticate()
        else:
            raise plugin.PluginError('Error: an api_key or email and password must be configured')

    def _authenticate(self) -> None:
        """Authenticate via email/password to get session cookie."""
        login_data = {
            'email': self.config.get('email'),
            'password': self.config.get('password'),
        }
        headers = self.create_json_headers()

        try:
            # Use the requests module directly since it handles cookies automatically
            resp = requests.post(
                self.base_url + '/auth/local',
                json=login_data,
                headers=headers,
                raise_status=False,
            )

            if resp.status_code != 200:
                log.error('Login failed with status %d: %s', resp.status_code, resp.text[:200])
                raise plugin.PluginError('Seerr email/password login failed')

            resp.raise_for_status()
            # Response body is user info (no token), but cookies are set automatically
            log.debug('Authenticated as user via cookie session')

        except RequestException as e:
            raise plugin.PluginError('Seerr login request failed') from e
        except HTTPError as e:
            raise plugin.PluginError('Seerr login HTTP error') from e

    def _request(self, method: str, endpoint: str, **params: Any) -> dict[str, Any]:
        """Make an HTTP request to the Seerr API."""
        if not endpoint.startswith('/'):
            endpoint = '/' + endpoint

        url = self.base_url + endpoint
        headers: dict[str, str] = params.pop('headers', {})

        # Apply auth headers/cookies
        if self._auth_mode == 'api_key':
            headers.update(self._auth_headers.copy())
        # Cookie auth is handled automatically by the requests module

        data = params.pop('data', None)

        resp = requests.request(
            method, url, params=params, headers=headers, raise_status=False, json=data
        )

        result = {}
        content_type = resp.headers.get('Content-Type', '')
        if 'application/json' in content_type:
            try:
                result = resp.json()
            except ValueError:
                result = {}

        try:
            resp.raise_for_status()
        except HTTPError as e:
            log.debug('API error %d: %s', e, result)
            raise

        return result

    def get(self, endpoint: str, **params: Any) -> dict[str, Any]:
        """GET request."""
        return self._request('get', endpoint, **params)

    def post(self, endpoint: str, **params: Any) -> dict[str, Any]:
        """POST request."""
        return self._request('post', endpoint, **params)

    def put(self, endpoint: str, **params: Any) -> dict[str, Any]:
        """PUT request."""
        return self._request('put', endpoint, **params)

    def delete(self, endpoint: str, **params: Any) -> dict[str, Any]:
        """DELETE request."""
        return self._request('delete', endpoint, **params)

    @classmethod
    def create_json_headers(cls) -> dict[str, str]:
        """Create JSON request headers."""
        return {'Content-Type': 'application/json', 'Accept': 'application/json'}


class SeerrEntry:
    """Represents a generic entry from the Seerr API (a request object).

    Seerr stores media details nested under 'media', and request metadata
    at the top level. Status is an integer:
        1=pending, 2=approved, 3=available, 4=declining, 5=deleted
    """

    def __init__(self, request_client: SeerrRequest, data: dict[str, Any]) -> None:
        self._request = request_client
        self.data = data
        media = data.get('media', {})
        self.seer_title: str = media.get('title', 'Unknown')
        self.entry_type = media.get('mediaType', 'movie')

        # Add season/episode suffix for TV
        if data.get('season'):
            self.seer_title += ' S' + str(data['season']).zfill(2)
        if data.get('episode'):
            self.seer_title += ' E' + str(data['episode']).zfill(2)

    @property
    def request_id(self) -> str:
        """Get the request ID."""
        return str(self.data.get('id', ''))

    @request_id.setter
    def request_id(self, value: str) -> None:
        self.data['id'] = value

    @property
    def status_code(self) -> int:
        """Get the integer status code."""
        return self.data.get('status', 0)

    @property
    def status_text(self) -> str:
        """Get the status as a text string."""
        return STATUS_TEXT.get(self.status_code, 'unknown')

    def already_requested(self) -> tuple[bool, str]:
        """Check if already in a terminal state.

        Returns:
            (bool, str): (is_terminal, status_text)
        """
        if self.status_code in (STATUS_AVAILABLE, STATUS_DELETED, STATUS_DECLINING):
            return True, self.status_text
        if self.status_code == STATUS_APPROVED:
            return True, self.status_text
        if self.status_code == STATUS_PENDING:
            return True, self.status_text
        return False, 'unrequested'

    def mark_requested(self, data: dict[str, Any]) -> bool:
        """Create a new request in Seerr.

        Seerr requires: {'mediaId': <id>, 'mediaType': 'movie'|'tv'}
        """
        log.info('Requesting {} in Seerr.', self.seer_title)

        try:
            response = self._request.post('/request', data=data)
            self.request_id = str(response.get('id', ''))
            log.info('{} was requested in Seerr.', self.seer_title)
            return True
        except (HTTPError, ApiError, ValueError) as e:
            log.error('Failed to mark {} as requested in Seerr.', self.seer_title)
            log.verbose(str(e))
            return False

    def mark_available(self) -> None:
        """Mark request as available."""
        if self.status_code == STATUS_AVAILABLE:
            log.verbose(f'{self.seer_title} already available in Seerr.')
            return
        log.info('Marking {} as available in Seerr.', self.seer_title)
        try:
            self._request.post(f'/request/{self.request_id}/available')
            log.info('{} has been marked available.', self.seer_title)
        except (HTTPError, ApiError) as e:
            log.error('Failed to mark {} as available in Seerr.', self.seer_title)
            log.debug(e)

    def mark_deleting(self) -> None:
        """Mark request for deletion (semi-automatic, needs Radarr/Sonarr)."""
        if self.status_code == STATUS_DELETED:
            log.verbose(f'{self.seer_title} already deleted in Seerr.')
            return
        log.info('Marking {} as deleting in Seerr.', self.seer_title)
        try:
            self._request.post(f'/request/{self.request_id}/deleting')
            log.info('{} has been marked deleting.', self.seer_title)
        except (HTTPError, ApiError) as e:
            log.error('Failed to mark {} as deleting in Seerr.', self.seer_title)
            log.debug(e)

    def mark_declined(self) -> None:
        """Decline a request."""
        if self.status_code == STATUS_DECLINING:
            log.verbose(f'{self.seer_title} already declined in Seerr.')
            return
        log.info('Marking {} as declined in Seerr.', self.seer_title)
        try:
            self._request.post(f'/request/{self.request_id}/declined')
            log.info('{} has been marked declined.', self.seer_title)
        except (HTTPError, ApiError) as e:
            log.error('Failed to mark {} as declined in Seerr.', self.seer_title)
            log.debug(e)

    def mark_pending(self) -> None:
        """Reopen/pending a request (undo decline/deletion)."""
        if self.status_code == STATUS_PENDING:
            log.verbose(f'{self.seer_title} already pending in Seerr.')
            return
        log.info('Marking {} as pending in Seerr.', self.seer_title)
        try:
            self._request.post(f'/request/{self.request_id}/pending')
            log.info('{} has been marked pending.', self.seer_title)
        except (HTTPError, ApiError) as e:
            log.error('Failed to mark {} as pending in Seerr.', self.seer_title)
            log.debug(e)


class SeerrMovie(SeerrEntry):
    """Manage a movie entry in Seerr."""

    entry_type = 'movie'

    @classmethod
    def from_tmdb_id(cls, request_client: SeerrRequest, tmdb_id: str) -> SeerrMovie | None:
        """Look up a movie by TMDB ID."""
        headers = request_client.create_json_headers()
        try:
            data = request_client.get(f'/movie/{tmdb_id}', headers=headers)
            return SeerrMovie(request_client, data)
        except (HTTPError, ApiError, KeyError) as e:
            log.error('Failed to get Seerr movie by tmdb_id: {}', tmdb_id)
            log.debug(e)
            return None

    @classmethod
    def from_imdb_id(cls, request_client: SeerrRequest, imdb_id: str) -> SeerrMovie | None:
        """Look up a movie by IMDB ID."""
        headers = request_client.create_json_headers()
        try:
            data = request_client.get(f'/search/imdb/{imdb_id}', headers=headers)
            return SeerrMovie(request_client, data)
        except (HTTPError, ApiError, KeyError) as e:
            log.error('Failed to get Seerr movie by imdb_id: {}', imdb_id)
            log.debug(e)
            return None

    @classmethod
    def from_id(cls, request_client: SeerrRequest, entry: Entry) -> SeerrMovie | None:
        """Create a SeerrEntry from a FlexGet entry."""
        if entry.get('tmdb_id'):
            return cls.from_tmdb_id(request_client, str(entry['tmdb_id']))
        if entry.get('imdb_id'):
            return cls.from_imdb_id(request_client, str(entry['imdb_id']))
        log.error('Entry has no tmdb_id or imdb_id to lookup in Seerr')
        return None


class SeerrTv(SeerrEntry):
    """Manage a TV entry in Seerr."""

    entry_type = 'tv'

    def __init__(self, request_client: SeerrRequest, data: dict[str, Any], sub_type: str) -> None:
        super().__init__(request_client, data)
        self.sub_type = sub_type

    @classmethod
    def from_tmdb_id(
        cls,
        request_client: SeerrRequest,
        entry: Entry,
        sub_type: Literal['shows', 'seasons', 'episodes'],
    ) -> SeerrTv | None:
        """Look up a TV show by TMDB ID."""
        headers = request_client.create_json_headers()
        if not entry.get('tmdb_id'):
            return None
        tmdb_id = str(entry['tmdb_id'])
        try:
            data = request_client.get(f'/tv/{tmdb_id}', headers=headers)
            entry.update(data)
            return SeerrTv(request_client, entry, sub_type)
        except (HTTPError, ApiError, KeyError) as e:
            log.error('Failed to get Seerr TV by tmdb_id: {}', tmdb_id)
            log.debug(e)
            return None


class SeerrSet(MutableSet):
    """The managed list for Seerr."""

    supported_ids = SUPPORTED_IDS
    schema = {
        'type': 'object',
        'properties': {
            'url': {'type': 'string'},
            'api_key': {'type': 'string'},
            'email': {'type': 'string'},
            'password': {'type': 'string'},
            'type': {'type': 'string', 'enum': ['shows', 'seasons', 'episodes', 'movies']},
            'status': {
                'type': 'string',
                'enum': ['approved', 'pending', 'available', 'all'],
                'default': 'approved',
            },
            'hide_available': {'type': 'boolean', 'default': True},
            'on_remove': {
                'type': 'string',
                'enum': ['pending', 'deleting', 'declined'],
                'default': 'deleting',
            },
            'include_year': {'type': 'boolean', 'default': False},
            'include_ep_title': {'type': 'boolean', 'default': False},
        },
        'oneOf': [{'required': ['email', 'password']}, {'required': ['api_key']}],
        'required': ['url', 'type'],
        'additionalProperties': False,
    }

    @property
    def immutable(self) -> bool:
        return False

    def __init__(self, config: dict[str, Any]) -> None:
        self.config: Config = config
        self._items: list[Entry] | None = None

    def __iter__(self):
        return iter(self.items)

    def __len__(self):
        return len(self.items)

    # -- MutableSet methods -- #

    def add(self, entry: Entry) -> None:
        """Add an entry to Seerr (create a new request)."""
        log.info('Adding {} to Seerr as {}.', entry['title'], self.config['status'])

        seerr_entry = self._get_seerr_entry(entry)
        if not seerr_entry:
            log.error('Failed to find SEERR entry for {}.', entry['title'])
            return

        already, status = seerr_entry.already_requested()
        if already:
            log.verbose(
                'Not marking %s as requested because it is already %s.',
                seerr_entry.seer_title,
                status,
            )
            self.invalidate_cache()
            return

        # Build the request payload
        media = seerr_entry.data.get('media', seerr_entry.data)
        media_type = media.get('mediaType', self.entry_type_from_config())

        request_data = {
            'mediaId': media.get('id'),
            'mediaType': media_type,
        }

        # Create the request
        seerr_entry.mark_requested(request_data)

        # Then apply the configured status action
        if self.config['status'] == 'pending':
            self.invalidate_cache()
            return

        action = f'mark_{self.config["status"]}'
        method = getattr(seerr_entry, action, None)
        if not method:
            log.error(
                'Cannot find action %s for status %s.',
                action,
                self.config['status'],
            )
            return

        method()
        self.invalidate_cache()

    def discard(self, entry: Entry) -> None:
        """Remove an entry from Seerr (change status)."""
        log.info('Removing {} from seer_list.', entry['title'])

        seerr_entry = self._get_seerr_entry(entry)
        if not seerr_entry:
            log.error('Failed to find SEERR entry for {}.', entry['title'])
            return

        on_remove = self.config.get('on_remove', 'deleting')
        action = f'mark_{on_remove}'
        method = getattr(seerr_entry, action, None)
        if not method:
            log.error(
                'Cannot find action %s for on_remove %s.',
                action,
                on_remove,
            )
            return

        method()
        self.invalidate_cache()

    def __contains__(self, entry) -> bool:
        return self._find_entry(entry) is not None

    def __ior__(self, entries: list[Entry]) -> SeerrSet:
        for entry in entries:
            self.add(entry)
        return self

    def __isub__(self, entries: list[Entry]) -> SeerrSet:
        for entry in entries:
            self.discard(entry)
        return self

    def get(self, entry: Entry) -> dict[str, Any] | None:
        return self._find_entry(entry)

    def invalidate_cache(self) -> None:
        self._items = None

    # -- Internal methods -- #

    @property
    def items(self) -> list[Entry]:
        """Get cached list of Seerr requests as FlexGet Entries."""
        if self._items is not None:
            return self._items

        raw_items = self.get_requested_items()
        self._items = []
        list_type = self.config['type']

        if list_type == 'movies':
            filtered = filter_seerr_items(raw_items, self.config)
            self._items = [self.generate_movie_entry(item) for item in filtered]

        elif list_type == 'shows':
            filtered = filter_seerr_items(raw_items, self.config)
            self._items = [
                self.generate_tv_entry(item, sub_type='shows')
                for item in filtered
            ]

        elif list_type == 'seasons':
            filtered = filter_seerr_items(raw_items, self.config)
            for show in filtered:
                for season_data in show.get('seasons', []):
                    entry = self.generate_tv_entry(show, sub_type='seasons', season=season_data)
                    if entry:
                        self._items.append(entry)

        elif list_type == 'episodes':
            filtered = filter_seerr_items(raw_items, self.config)
            for show in filtered:
                for season_data in show.get('seasons', []):
                    for episode_data in season_data.get('episodes', []):
                        entry = self.generate_tv_entry(
                            show, sub_type='episodes', season=season_data, episode=episode_data
                        )
                        if entry:
                            self._items.append(entry)

        return self._items

    @property
    def online(self) -> bool:
        return True

    def _find_entry(self, entry: Entry) -> dict[str, Any] | None:
        find_method = getattr(self, f'_find_{self.config["type"]}', None)
        if not find_method:
            raise plugin.PluginError(
                'Unknown list type {}.'.format(self.config.get('type'))
            )
        return find_method(entry)

    def _find_movies(self, entry: Entry) -> dict[str, Any] | None:
        """Match a movie entry by its IDs."""
        for item in self.items:
            for id_type in SUPPORTED_IDS:
                if entry.get(id_type) and item.get(id_type) == entry.get(id_type):
                    return item
        return None

    def _find_shows(self, entry: Entry) -> dict[str, Any] | None:
        """Match a show entry by its IDs."""
        for item in self.items:
            for id_type in SUPPORTED_IDS:
                if entry.get(id_type) and item.get(id_type) == entry.get(id_type):
                    return item
        return None

    def _find_seasons(self, entry: Entry) -> dict[str, Any] | None:
        """Match a season by show ID + season number."""
        for item in self.items:
            for id_type in SUPPORTED_IDS:
                if (
                    entry.get(id_type)
                    and item.get(id_type) == entry.get(id_type)
                    and entry.get('tmdb_season') == item.get('tmdb_season')
                ):
                    return item
        return None

    def _find_episodes(self, entry: Entry) -> dict[str, Any] | None:
        """Match an episode by show ID + season + episode."""
        for item in self.items:
            for id_type in SUPPORTED_IDS:
                if (
                    entry.get(id_type)
                    and item.get(id_type) == entry.get(id_type)
                    and entry.get('tmdb_season') == item.get('tmdb_season')
                    and entry.get('tmdb_episode') == item.get('tmdb_episode')
                ):
                    return item
        return None

    def _get_seerr_entry(self, entry: Entry) -> SeerrMovie | SeerrTv | None:
        """Get a SeerrEntry from a FlexGet entry by looking up its IDs."""
        entry_type = self.config['type']
        request_client = SeerrRequest(self.config)

        if entry_type == 'movies':
            return SeerrMovie.from_id(request_client, entry)
        return SeerrTv.from_tmdb_id(request_client, entry, entry_type)

    def entry_type_from_config(self) -> str:
        """Map config type to Seerr mediaType string."""
        mapping = {
            'movies': 'movie',
            'shows': 'tv',
            'seasons': 'tv',
            'episodes': 'tv',
        }
        return mapping.get(self.config['type'], 'movie')

    def generate_series_id(self, season: dict, episode: dict | None = None) -> str:
        """Generate a series ID like S01E02."""
        num = season.get('seasonNumber', season.get('number', 1))
        tempid = 'S' + str(num).zfill(2)
        if episode:
            enum = episode.get('episodeNumber', episode.get('number', 1))
            tempid += 'E' + str(enum).zfill(2)
        return tempid

    def generate_title(
        self, item: dict, season: dict | None = None, episode: dict | None = None
    ) -> str:
        """Build a display title for the entry."""
        media = item.get('media', item)
        title = media.get('title', 'Unknown')

        # Add year
        release_date = media.get('releaseDate', media.get('release_date', ''))
        if release_date and self.config.get('include_year'):
            try:
                title = f'{title} ({release_date[:4]})'
            except (TypeError, IndexError):
                pass

        # Add season/episode
        if season or episode:
            title += ' ' + self.generate_series_id(season if season else {})
            if episode and episode.get('title') and self.config.get('include_ep_title'):
                title += ' ' + episode['title']

        return title

    def get_requested_items(self) -> list[dict[str, Any]]:
        """Fetch all requests from Seerr."""
        client = SeerrRequest(self.config)
        log.debug('Connecting to Seerr to retrieve requests.')

        try:
            headers = client.create_json_headers()
            response = client.get('/request', headers=headers)

            if isinstance(response, dict) and 'results' in response:
                return response['results']
            if isinstance(response, list):
                return response
            log.warning('Unexpected Seerr response format: %s', type(response))
            return []
        except (HTTPError, ApiError) as e:
            raise plugin.PluginError('Error retrieving requests from Seerr') from e

    def generate_movie_entry(self, parent_request: dict[str, Any]) -> Entry:
        """Convert a Seerr request object to a FlexGet Entry for movies."""
        media = parent_request.get('media', {})
        release_date = media.get('releaseDate', media.get('release_date', ''))
        movie_year = int(release_date[:4]) if release_date else 0

        tmdb_id = str(media.get('tmdbId', '')) or None
        imdb_id = str(media.get('imdbId', '')) or None
        tvdb_id = str(media.get('tvdbId', '')) or None

        url = f'http://www.imdb.com/title/{imdb_id}/' if imdb_id else ''
        title = self.generate_title(parent_request)

        return Entry(
            title=title,
            url=url,
            tmdb_id=tmdb_id if tmdb_id else None,
            imdb_id=imdb_id if imdb_id else None,
            tvdb_id=tvdb_id if tvdb_id else None,
            seer_id=str(media.get('id', '')),
            movie_name=media.get('title', ''),
            movie_year=movie_year,
            seer_request_id=str(parent_request.get('id', '')),
            seer_status=parent_request.get('status'),
            seer_status_text=STATUS_TEXT.get(parent_request.get('status'), 'unknown'),
            seer_type='movie',
            seer_poster_path=media.get('posterPath', media.get('poster_path', '')),
            seer_backdrop_path=media.get('backdropPath', media.get('backdrop_path', '')),
            seer_media_id=str(media.get('id', '')),
            seer_approved=(parent_request.get('status') == STATUS_APPROVED),
            seer_available=(parent_request.get('status') == STATUS_AVAILABLE),
            seer_pending=(parent_request.get('status') == STATUS_PENDING),
            seer_declined=(parent_request.get('status') in (STATUS_DECLINING, STATUS_DELETED)),
            # Additional Seerr-specific fields
            seer_rating_key=media.get('ratingKey', ''),
            seer_media_url=media.get('mediaUrl', ''),
            seer_external_service_id=str(media.get('externalServiceId', '')) or None,
            seer_created_at=parent_request.get('createdAt', ''),
            seer_updated_at=parent_request.get('updatedAt', ''),
        )

    def generate_tv_entry(
        self,
        parent_request: dict,
        sub_type: Literal['shows', 'seasons', 'episodes'] = 'shows',
        season: dict | None = None,
        episode: dict | None = None,
    ) -> Entry | None:
        """Convert a Seerr request to a FlexGet Entry for TV."""
        media = parent_request.get('media', parent_request)
        release_date = media.get('releaseDate', media.get('release_date', ''))
        tv_year = int(release_date[:4]) if release_date else 0

        tmdb_id = str(media.get('tmdbId', '')) or None
        imdb_id = str(media.get('imdbId', '')) or None
        tvdb_id = str(media.get('tvdbId', '')) or None

        url = f'http://www.imdb.com/title/{imdb_id}/' if imdb_id else ''
        title = self.generate_title(parent_request, season, episode)
        series_name = media.get('title', 'Unknown')

        base = {
            'title': title,
            'url': url,
            'tmdb_id': tmdb_id if tmdb_id else None,
            'imdb_id': imdb_id if imdb_id else None,
            'tvdb_id': tvdb_id if tvdb_id else None,
            'seer_id': str(media.get('id', '')),
            'series_name': series_name,
            'movie_year': tv_year,
            'seer_request_id': str(parent_request.get('id', '')),
            'seer_status': parent_request.get('status'),
            'seer_status_text': STATUS_TEXT.get(parent_request.get('status'), 'unknown'),
            'seer_type': 'tv',
            'seer_poster_path': media.get('posterPath', media.get('poster_path', '')),
            'seer_backdrop_path': media.get('backdropPath', media.get('backdrop_path', '')),
            'seer_media_id': str(media.get('id', '')),
            'seer_approved': parent_request.get('status') == STATUS_APPROVED,
            'seer_available': parent_request.get('status') == STATUS_AVAILABLE,
            'seer_pending': parent_request.get('status') == STATUS_PENDING,
            'seer_declined': parent_request.get('status') in (STATUS_DECLINING, STATUS_DELETED),
            'seer_rating_key': media.get('ratingKey', ''),
            'seer_media_url': media.get('mediaUrl', ''),
        }

        if sub_type == 'shows':
            return Entry(**{**base, 'series_name': title})

        if sub_type == 'seasons':
            snum = season.get('seasonNumber', 1) if season else 1
            return Entry(**{
                **base,
                'series_season': snum,
                'series_id': self.generate_series_id(season if season else {}),
                'tmdb_season': snum,
                'seer_season_id': str(season.get('id', '')) if season else '',
                'seer_season': snum,
            })

        if sub_type == 'episodes':
            if not season or not episode:
                return None
            snum = season.get('seasonNumber', 1)
            enum = episode.get('episodeNumber', 1)
            return Entry(**{
                **base,
                'series_season': snum,
                'series_episode': enum,
                'series_id': self.generate_series_id(season, episode),
                'tmdb_season': snum,
                'tmdb_episode': enum,
                'seer_season_id': str(season.get('id', '')),
                'seer_season': snum,
                'seer_episode_id': str(episode.get('id', '')),
                'seer_episode': enum,
                'seer_episode_title': episode.get('title', ''),
            })

        raise plugin.PluginError(
            'Unknown TV sub-type {}.'.format(sub_type)
        )


class SeerrList:
    """Seerr managed list plugin."""

    schema = SeerrSet.schema

    def get_list(self, config: dict[str, Any]) -> SeerrSet:
        return SeerrSet(config)

    def on_task_input(self, task, config: dict[str, Any]) -> list[Entry]:
        return list(SeerrSet(config))


@event('plugin.register')
def register_plugin() -> None:
    plugin.register(SeerrList, 'seer_list', api_ver=2, interfaces=['task', 'list'])


def filter_seerr_items(
    items: list[dict[str, Any]], config: Config
) -> list[dict[str, Any]]:
    """Filter Seerr requests based on config.

    Seerr uses integer status codes:
        1=pending, 2=approved, 3=available, 4=declining, 5=deleted
    """
    filtered = items

    # Hide available items if configured
    if config.get('hide_available', True):
        filtered = [
            item for item in filtered
            if item.get('status') != STATUS_AVAILABLE
        ]

    status = config.get('status', 'all')

    if status == 'all':
        return filtered
    if status == 'approved':
        return [i for i in filtered if i.get('status') == STATUS_APPROVED]
    if status == 'pending':
        return [i for i in filtered if i.get('status') == STATUS_PENDING]
    if status == 'available':
        return [i for i in filtered if i.get('status') == STATUS_AVAILABLE]

    raise plugin.PluginError(
        'Error: Unknown status {}.'.format(status)
    )
