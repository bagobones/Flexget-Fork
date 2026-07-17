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

# Type hinting for request type
SUPPORTED_IDS: list[Literal['tmdb_id', 'imdb_id', 'tvdb_id', 'seer_id']] = ['tmdb_id', 'imdb_id', 'tvdb_id', 'seer_id']

REQUEST_STATUS = Literal['pending', 'approved', 'available', 'declined', 'deleting']


class ApiError(Exception):
    """Exception raised when an API call fails."""

    def __init__(self, response: dict[str, Any]) -> None:
        self.response = response
        super().__init__(self.response.get('message', response.get('error', 'API Error')))


class Config(TypedDict):
    """The config schema for the Seerr managed list."""

    url: str
    api_key: NotRequired[str]
    username: NotRequired[str]
    password: NotRequired[str]
    type: Literal['shows', 'seasons', 'episodes', 'movies']
    status: Literal['approved', 'pending', 'available', 'all']
    hide_available: bool
    on_remove: NotRequired[Literal['pending', 'deleting', 'declined']]
    include_year: bool
    include_ep_title: bool


class SeerrRequest:
    """HTTP client for the Seerr API."""

    def __init__(self, config: Config) -> None:
        self.base_url = config['url'].rstrip('/')
        self.config: Config = config
        self.auth_header = self._create_auth_header()

    def _create_auth_header(self) -> dict[str, str]:
        """Create authentication headers based on config."""
        if 'api_key' in self.config:
            log.debug('Authenticating via api_key')
            api_key = self.config['api_key']
            return {'X-Api-Key': api_key}

        if self.config.get('username') and self.config.get('password'):
            log.debug('Authenticating via username/password')
            access_token = self._get_access_token()
            return {'Authorization': f'Bearer {access_token}'}

        raise plugin.PluginError('Error: an api_key or username and password must be configured')

    def _get_access_token(self) -> str:
        """Get access token via username/password login."""
        endpoint = '/auth/local'
        data = {
            'username': self.config.get('username'),
            'password': self.config.get('password'),
        }
        headers = self.create_json_headers()
        try:
            response = self._request('post', endpoint, data=data, headers=headers)
            return response.get('token', response.get('accessToken', ''))
        except (HTTPError, RequestException, ValueError) as e:
            raise plugin.PluginError('Seerr username and password login failed') from e

    def _request(self, method: str, endpoint: str, **params: Any) -> dict[str, Any]:
        """Make an HTTP request to the Seerr API."""
        if not endpoint.startswith('/'):
            endpoint = '/' + endpoint

        url = self.base_url + endpoint

        headers: dict[str, str] = params.pop('headers', {})
        data = params.pop('data', None)

        # Add auth header
        headers.update(self.auth_header.copy())

        response = requests.request(
            method, url, params=params, headers=headers, raise_status=False, json=data
        )

        result = {}

        # Parse JSON response
        if 'application/json' in response.headers.get('Content-Type', ''):
            try:
                result = response.json()
            except ValueError:
                result = {}

        try:
            response.raise_for_status()
        except HTTPError as e:
            log.debug('API error: %s - %s', e, result)
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
    """Represents a generic entry returned from the Seerr API."""

    def __init__(self, request_client: SeerrRequest, entry_type: str, data: dict[str, Any]) -> None:
        self._request = request_client
        self.entry_type = entry_type
        self.data = data
        self.seer_title: str = data.get('media', {}).get('title', 'Unknown')

        # Handle season/episode suffix
        if data.get('season'):
            self.seer_title = self.seer_title + ' S' + str(data['season']).zfill(2)
        if data.get('episode'):
            self.seer_title = self.seer_title + ' E' + str(data['episode']).zfill(2)

    @property
    def request_id(self) -> str:
        """Get the request ID from the entry."""
        return str(self.data.get('id', ''))

    @request_id.setter
    def request_id(self, value: str) -> None:
        """Set the request ID."""
        self.data['id'] = value

    def already_requested(self) -> tuple[bool, str]:
        """Check if an entry in Seerr has already been requested.

        Returns:
            tuple[bool, str]: (is_requested, status_string)
        """
        request_status = self.data.get('requestStatus', '')
        if request_status in ('approved', 'available', 'pending', 'deleting'):
            return True, request_status

        return False, 'unrequested'

    def mark_requested(self, endpoint: str, data: dict[str, Any]) -> bool:
        """Mark an entry in Seerr as being requested."""
        log.info('Requesting {} in Seerr.', self.seer_title)

        headers = self._request.create_json_headers()

        try:
            response: dict[str, Any] = self._request.post(
                endpoint=endpoint, data=data, headers=headers
            )
            self.request_id = str(response.get('id', ''))
            log.info('{} was requested in Seerr.', self.seer_title)
            return True
        except (HTTPError, ApiError) as error:
            if isinstance(error, ApiError):
                error_msg = error.response.get('message', '').lower()
                if 'already' in error_msg or 'exists' in error_msg:
                    log.verbose(f'{self.seer_title} already requested in Seerr.')
                    return True

            log.error('Failed to mark {} as requested in Seerr.', self.seer_title)
            log.verbose(error.response)
            return False
        return True

    def mark_available(self) -> None:
        """Mark an entry in Seerr as available."""
        if self.data.get('requestStatus') == 'available':
            log.verbose(f'{self.seer_title} already available in Seerr.')
            return

        log.info('Marking {} as available in Seerr.', self.seer_title)

        api_endpoint = f'/request/{self.request_id}/available'

        try:
            self._request.post(api_endpoint)
            log.info('{} has been marked available.', self.seer_title)
        except (HTTPError, ApiError) as e:
            log.error('Failed to mark {} as available in Seerr.', self.seer_title)
            log.debug(e)

    def mark_deleting(self) -> None:
        """Mark an entry in Seerr as deleting."""
        if self.data.get('requestStatus') == 'deleting':
            log.verbose(f'{self.seer_title} already deleting in Seerr.')
            return

        log.info('Marking {} as deleting in Seerr.', self.seer_title)

        api_endpoint = f'/request/{self.request_id}/deleting'

        try:
            self._request.post(api_endpoint)
            log.info('{} has been marked deleting.', self.seer_title)
        except (HTTPError, ApiError) as e:
            log.error('Failed to mark {} as deleting in Seerr.', self.seer_title)
            log.debug(e)

    def mark_declined(self) -> None:
        """Mark an entry in Seerr as declined."""
        if self.data.get('requestStatus') == 'declined':
            log.verbose(f'{self.seer_title} already declined in Seerr.')
            return

        log.info('Marking {} as declined in Seerr.', self.seer_title)

        api_endpoint = f'/request/{self.request_id}/declined'

        try:
            self._request.post(api_endpoint)
            log.info('{} has been marked declined.', self.seer_title)
        except (HTTPError, ApiError) as e:
            log.error('Failed to mark {} as declined in Seerr.', self.seer_title)
            log.debug(e)

    def mark_pending(self) -> None:
        """Mark an entry in Seerr as pending (reopen request)."""
        if self.data.get('requestStatus') == 'pending':
            log.verbose(f'{self.seer_title} already pending in Seerr.')
            return

        log.info('Marking {} as pending in Seerr.', self.seer_title)

        api_endpoint = f'/request/{self.request_id}/pending'

        try:
            self._request.post(api_endpoint)
            log.info('{} has been marked pending.', self.seer_title)
        except (HTTPError, ApiError) as e:
            log.error('Failed to mark {} as pending in Seerr.', self.seer_title)
            log.debug(e)


class SeerrMovie(SeerrEntry):
    """Manage a Movie entry in Seerr."""

    entry_type = 'movie'

    def __init__(self, request_client: SeerrRequest, data: dict[str, Any]) -> None:
        super().__init__(request_client, self.entry_type, data)

    def mark_requested(self) -> bool:
        """Mark a movie entry in Seerr as requested."""
        already_requested, status = self.already_requested()
        if already_requested:
            log.verbose(
                f'Not marking {self.seer_title} as requested in Seerr because it is already {status}.'
            )
            return True

        api_endpoint = '/request'

        # Seerr uses media.tmdbId for movie requests
        data = {'mediaId': self.data.get('media', {}).get('id')}

        return super().mark_requested(api_endpoint, data)

    @classmethod
    def from_tmdb_id(cls, request_client: SeerrRequest, tmdb_id: str) -> SeerrMovie | None:
        """Create a Seerr Entry from a TMDB ID."""
        headers = request_client.create_json_headers()
        endpoint = f'/search/moviedb/{tmdb_id}'

        try:
            data = request_client.get(endpoint, headers=headers)
            return SeerrMovie(request_client, data)
        except (HTTPError, ApiError, KeyError) as e:
            log.error('Failed to get Seerr movie by tmdb_id: {}', tmdb_id)
            log.debug(e)
            return None

    @classmethod
    def from_imdb_id(cls, request_client: SeerrRequest, imdb_id: str) -> SeerrMovie | None:
        """Create a Seerr Entry from an IMDB ID."""
        headers = request_client.create_json_headers()
        endpoint = f'/search/imdb/{imdb_id}'

        try:
            data = request_client.get(endpoint, headers=headers)
            return SeerrMovie(request_client, data)
        except (HTTPError, ApiError, KeyError) as e:
            log.error('Failed to get Seerr movie by imdb_id: {}', imdb_id)
            log.debug(e)
            return None

    @classmethod
    def from_id(cls, request_client: SeerrRequest, entry: Entry) -> SeerrMovie | None:
        """Create a Seerr Entry from a FlexGet entry with an ID."""
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
        super().__init__(request_client, self.entry_type, data)
        self.sub_type = sub_type

    def mark_requested(self) -> bool:
        """Mark a TV entry in Seerr as requested."""
        api_endpoint = '/request'

        payload: dict[str, Any] = {'mediaId': self.data.get('media', {}).get('id')}

        if self.sub_type == 'seasons':
            payload['seasons'] = [self.data.get('season', 1)]
        elif self.sub_type == 'episodes':
            payload['seasons'] = [
                {
                    'number': self.data.get('season', 1),
                    'episodes': [self.data.get('episode', 1)],
                }
            ]

        return super().mark_requested(api_endpoint, payload)

    @classmethod
    def from_tmdb_id(
        cls,
        request_client: SeerrRequest,
        entry: Entry,
        sub_type: Literal['shows', 'seasons', 'episodes'],
    ) -> SeerrTv | None:
        """Create a Seerr Entry from a TMDB ID."""
        headers = request_client.create_json_headers()

        if not entry.get('tmdb_id'):
            return None

        tmdb_id = str(entry['tmdb_id'])
        endpoint = f'/search/tvdb/{tmdb_id}'

        try:
            data = request_client.get(endpoint, headers=headers)
            entry.update(data)
            return SeerrTv(request_client, entry, sub_type)
        except (HTTPError, ApiError, KeyError) as e:
            log.error('Failed to get Seerr TV by tmdb_id: {}', tmdb_id)
            log.debug(e)
            return None


class SeerrSet(MutableSet):
    """The schema for the Seerr managed list."""

    supported_ids = SUPPORTED_IDS
    schema = {
        'type': 'object',
        'properties': {
            'url': {'type': 'string'},
            'api_key': {'type': 'string'},
            'username': {'type': 'string'},
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
        'oneOf': [{'required': ['username', 'password']}, {'required': ['api_key']}],
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

    def add(self, entry: Entry) -> None:
        """Add an entry to Seerr (request it)."""
        log.info('Adding {} to Seerr as {}.', entry['title'], self.config['status'])

        log.debug('Getting SEERR entry for {}.', entry['title'])

        seerr_entry = self._get_seerr_entry(entry)

        if not seerr_entry:
            log.error('Failed to find SEERR entry for {}.', entry['title'])
            return

        already_requested, status = seerr_entry.already_requested()
        if already_requested:
            log.verbose(
                f'Not marking {seerr_entry.seer_title} as requested in Seerr because it is already {status}.'
            )
            self.invalidate_cache()
            return

        # Mark as requested first
        seerr_entry.mark_requested()

        if self.config['status'] == 'pending':
            self.invalidate_cache()
            return

        # Get the correct method based on config status
        status_method = f'mark_{self.config["status"]}'
        mark_method = getattr(seerr_entry, status_method, None)

        if not mark_method:
            log.error(
                'Failed to find correct method to mark {} as {}.',
                entry['title'],
                self.config['status'],
            )
            return

        mark_method()

        self.invalidate_cache()

    def __ior__(self, entries: list[Entry]) -> SeerrSet:
        for entry in entries:
            self.add(entry)
        return self

    def discard(self, entry: Entry) -> None:
        """Remove an entry from Seerr."""
        log.info('Removing {} from seer_list.', entry['title'])

        log.debug('Getting SEERR entry for {}.', entry['title'])

        seerr_entry = self._get_seerr_entry(entry)

        if not seerr_entry:
            log.error('Failed to find SEERR entry for {}.', entry['title'])
            return

        # Map on_remove config to method names
        on_remove = self.config.get('on_remove', 'deleting')
        unmark_method = getattr(seerr_entry, f'mark_{on_remove}', None)

        if not unmark_method:
            log.error(
                'Failed to find correct method to mark {} as {}.',
                entry['title'],
                on_remove,
            )
            return

        unmark_method()

        self.invalidate_cache()

    def __isub__(self, entries: list[Entry]) -> SeerrSet:
        for entry in entries:
            self.discard(entry)
        return self

    def _find_entry(self, entry: Entry) -> dict[str, Any] | None:
        """Find an entry in the Seerr list by matching IDs."""
        find_method = getattr(self, f'_find_{self.config["type"]}', None)

        if not find_method:
            raise plugin.PluginError(
                'Error: Unknown list type {}.'.format(self.config.get('type'))
            )

        return find_method(entry)

    def __contains__(self, entry) -> bool:
        return self._find_entry(entry) is not None

    def invalidate_cache(self) -> None:
        self._items = None

    def get(self, entry: Entry) -> dict[str, Any] | None:
        return self._find_entry(entry)

    @property
    def items(self) -> list[Entry]:
        """Get the list of items from Seerr, cached."""
        if self._items is not None:
            return self._items

        requested_items = self.get_requested_items()

        self._items = []
        list_type = self.config['type']

        if list_type == 'movies':
            filtered_items = filter_seerr_items(requested_items, self.config)
            self._items = [self.generate_movie_entry(item) for item in filtered_items]
            return self._items

        if list_type == 'shows':
            shows = [self.generate_tv_entry(item, sub_type='shows') for item in requested_items]
            self._items = shows
            return self._items

        if list_type == 'seasons':
            seasons = []
            for show in requested_items:
                for season_data in show.get('seasons', []):
                    season_entry = self.generate_tv_entry(
                        show, sub_type='seasons', season=season_data
                    )
                    if season_entry:
                        seasons.append(season_entry)
            self._items = seasons
            return self._items

        if list_type == 'episodes':
            episodes = []
            for show in requested_items:
                for season_data in show.get('seasons', []):
                    for episode_data in season_data.get('episodes', []):
                        ep_entry = self.generate_tv_entry(
                            show, sub_type='episodes', season=season_data, episode=episode_data
                        )
                        if ep_entry:
                            episodes.append(ep_entry)
            # Filter episodes by status
            filtered_episodes = filter_seerr_items(episodes, self.config)
            self._items = [ep for ep in filtered_episodes if isinstance(ep, Entry)]
            return self._items

        raise plugin.PluginError('Error: Unknown list type {}.'.format(self.config.get('type')))

    @property
    def online(self) -> bool:
        """Seerr is always considered an online plugin."""
        return True

    # -- Find methods -- #

    def _find_movies(self, entry: Entry) -> dict[str, Any] | None:
        """Search for a movie entry by matching IDs."""
        log.debug('Doing a movie search in Seerr.')
        for item in self.items:
            for id_type in SUPPORTED_IDS:
                if entry.get(id_type) and item.get(id_type) == entry.get(id_type):
                    return item
        return None

    def _find_shows(self, entry: Entry) -> dict[str, Any] | None:
        """Search for a show entry by matching IDs."""
        log.debug('Doing a show search in Seerr.')
        for item in self.items:
            for id_type in SUPPORTED_IDS:
                if entry.get(id_type) and item.get(id_type) == entry.get(id_type):
                    return item
        return None

    def _find_seasons(self, entry: Entry) -> dict[str, Any] | None:
        """Search for a season entry by matching show ID and season number."""
        log.debug('Doing a season search in Seerr.')
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
        """Search for an episode entry by matching show ID, season, and episode."""
        log.debug('Doing an episode search in Seerr.')
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
        """Get a Seerr entry object from a FlexGet entry."""
        entry_type = self.config['type']
        request_client = SeerrRequest(self.config)

        if entry_type == 'movies':
            return SeerrMovie.from_id(request_client, entry)
        return SeerrTv.from_tmdb_id(request_client, entry, entry_type)

    # -- Helper methods -- #

    def generate_series_id(self, season: dict, episode: dict | None = None) -> str:
        """Generate a series ID string like S01E02."""
        tempid = 'S' + str(season.get('number', season.get('seasonNumber', 1))).zfill(2)
        if episode:
            tempid = tempid + 'E' + str(episode.get('number', episode.get('episodeNumber', 1))).zfill(2)
        return tempid

    def generate_title(self, item: dict, season: dict | None = None, episode: dict | None = None) -> str:
        """Generate a display title for an entry."""
        media = item.get('media', item)
        temptitle = media.get('title', 'Unknown')

        # Add year if requested
        release_date = media.get('releaseDate', media.get('release_date', ''))
        if release_date and self.config.get('include_year'):
            try:
                temptitle = f'{temptitle} ({release_date[:4]})'
            except (TypeError, IndexError):
                pass

        # Add season/episode info
        if season or episode:
            temptitle += ' ' + self.generate_series_id(season, episode)
            if episode and episode.get('title') and self.config.get('include_ep_title'):
                temptitle += ' ' + episode['title']

        return temptitle

    def get_requested_items(self) -> list[dict[str, Any]]:
        """Get all requested items from Seerr."""
        request_client = SeerrRequest(self.config)
        log.debug('Connecting to Seerr to retrieve list of requests.')

        try:
            headers = request_client.create_json_headers()
            response = request_client.get('/request', headers=headers)

            # Seerr returns a paginated response - handle both formats
            if isinstance(response, dict) and 'results' in response:
                items = response['results']
            elif isinstance(response, list):
                items = response
            else:
                log.warning('Unexpected response format from Seerr: %s', type(response))
                return []

            return items
        except (HTTPError, ApiError) as error:
            raise plugin.PluginError('Error retrieving list of requests from Seerr') from error

    def generate_movie_entry(self, parent_request: dict[str, Any]) -> Entry:
        """Generate a FlexGet Entry from a Seerr movie request."""
        media = parent_request.get('media', {})
        release_date = media.get('releaseDate', media.get('release_date', ''))
        movie_year = int(release_date[:4]) if release_date else 0

        imdb_id = media.get('imdbId', media.get('imdb_id', ''))
        tmdb_id = media.get('tmdbId', media.get('tmdb_id', ''))
        tvdb_id = media.get('tvdbId', media.get('tvdb_id', ''))

        # Build IMDb URL
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
            seer_status=parent_request.get('requestStatus', ''),
            seer_approved=parent_request.get('requestStatus') == 'approved',
            seer_available=parent_request.get('requestStatus') == 'available',
            seer_pending=parent_request.get('requestStatus') == 'pending',
            seer_declined=parent_request.get('requestStatus') == 'declined',
            seer_type='movie',
            seer_poster_path=media.get('posterPath', media.get('poster_path', '')),
            seer_backdrop_path=media.get('backdropPath', media.get('backdrop_path', '')),
            seer_media_id=str(media.get('id', '')),
        )

    def generate_tv_entry(
        self,
        parent_request: dict,
        sub_type: Literal['shows', 'seasons', 'episodes'] = 'shows',
        season: dict | None = None,
        episode: dict | None = None,
    ) -> Entry | None:
        """Generate a FlexGet Entry from a Seerr TV request."""
        media = parent_request.get('media', parent_request)
        release_date = media.get('releaseDate', media.get('release_date', ''))
        tv_year = int(release_date[:4]) if release_date else 0

        imdb_id = media.get('imdbId', media.get('imdb_id', ''))
        tmdb_id = media.get('tmdbId', media.get('tvdbId', media.get('tmdb_id', '')))
        tvdb_id = media.get('tvdbId', media.get('tvdb_id', ''))

        url = f'http://www.imdb.com/title/{imdb_id}/' if imdb_id else ''
        title = self.generate_title(parent_request, season, episode)
        series_name = media.get('title', 'Unknown')

        base_entry = {
            'title': title,
            'url': url,
            'tmdb_id': tmdb_id if tmdb_id else None,
            'imdb_id': imdb_id if imdb_id else None,
            'tvdb_id': tvdb_id if tvdb_id else None,
            'seer_id': str(media.get('id', '')),
            'series_name': series_name,
            'movie_year': tv_year,
            'seer_request_id': str(parent_request.get('id', '')),
            'seer_status': parent_request.get('requestStatus', ''),
            'seer_type': 'tv',
            'seer_poster_path': media.get('posterPath', media.get('poster_path', '')),
            'seer_backdrop_path': media.get('backdropPath', media.get('backdrop_path', '')),
            'seer_media_id': str(media.get('id', '')),
        }

        if sub_type == 'shows':
            return Entry(**{
                **base_entry,
                'series_name': title,
                'seer_show_id': str(media.get('id', '')),
            })

        if sub_type == 'seasons':
            season_num = season.get('number', season.get('seasonNumber', 1)) if season else 1
            return Entry(**{
                **base_entry,
                'series_name': series_name,
                'series_season': season_num,
                'series_id': self.generate_series_id(season if season else {}),
                'tmdb_season': season_num,
                'seer_season_id': str(season.get('id', '')) if season else '',
                'seer_season': season_num,
            })

        if sub_type == 'episodes':
            if not season or not episode:
                return None
            season_num = season.get('number', season.get('seasonNumber', 1))
            episode_num = episode.get('number', episode.get('episodeNumber', 1))
            ep_title = episode.get('title', '')

            return Entry(**{
                **base_entry,
                'series_name': series_name,
                'series_season': season_num,
                'series_episode': episode_num,
                'series_id': self.generate_series_id(season, episode),
                'tmdb_season': season_num,
                'tmdb_episode': episode_num,
                'seer_season_id': str(season.get('id', '')),
                'seer_season': season_num,
                'seer_episode_id': str(episode.get('id', '')),
                'seer_episode': episode_num,
                'seer_episode_title': ep_title,
            })

        raise plugin.PluginError('Error: Unknown TV sub-type {}.'.format(sub_type))


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


def filter_seerr_items(items: list[dict[str, Any]], config: Config) -> list[dict[str, Any]]:
    """Filter Seerr items based on the config.

    Arguments:
        items: The items returned from the Seerr API.
        config: The config for the Seerr managed list.

    Returns:
        list[dict[str, Any]]: The filtered list of items.
    """
    filtered_items = items

    # Hide available items if configured
    if config.get('hide_available', True):
        filtered_items = [
            item for item in filtered_items
            if item.get('requestStatus') != 'available'
        ]

    # Filter by status
    status = config.get('status', 'all')

    if status == 'all':
        return filtered_items

    if status == 'approved':
        return [item for item in filtered_items if item.get('requestStatus') == 'approved']

    if status == 'pending':
        return [item for item in filtered_items if item.get('requestStatus') == 'pending']

    if status == 'available':
        return [item for item in filtered_items if item.get('requestStatus') == 'available']

    raise plugin.PluginError('Error: Unknown status {}.'.format(status))
