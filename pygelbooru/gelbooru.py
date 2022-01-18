import asyncio
import os
import reprlib
import xml
from random import randint
from typing import *
from urllib.parse import urlparse

import aiohttp
import xmltodict
from furl import furl


class GelbooruException(Exception):
    pass


class GelbooruNotFoundException(GelbooruException):
    pass


class GelbooruImage:
    """
    Container for Gelbooru image results.
    Returns the image URL when cast to str
    """

    def __init__(self, payload: dict, gelbooru):
        self._gelbooru = gelbooru  # type: Gelbooru

        self.id             = int(payload.get('id'))
        self.creator_id     = payload.get('creator_id')
        self.created_at     = payload.get('created_at')
        self.file_url       = payload.get('file_url')
        self.filename       = os.path.basename(urlparse(self.file_url).path)
        self.source         = payload.get('source') or None
        self.hash           = payload.get('hash')
        self.height         = int(payload.get('height'))
        self.width          = int(payload.get('width'))
        self.rating         = payload.get('rating')
        self.has_sample     = payload.get('sample')
        self.has_comments   = payload.get('has_comments')
        self.has_notes      = payload.get('has_notes')
        self.tags           = str(payload.get('tags')).split(' ')
        self.change         = payload.get('change')
        self.directory      = payload.get('directory')

        self._comments = []  # type: List[GelbooruComment]

    async def get_comments(self):
        if not self.has_comments:
            return []

        # Already cached?
        if self._comments:
            return self._comments

        self._comments = await self._gelbooru.get_comments(self)
        return self._comments

    def __str__(self):
        return self.file_url

    def __int__(self):
        return self.id

    def __repr__(self):
        rep = reprlib.Repr()
        return f"<GelbooruImage(id={self.id}, filename={rep.repr(self.filename)}, owner={rep.repr(self.creator_id)})>"


class GelbooruTag:
    """
    Container for Gelbooru tag results.
    Returns the tag name when cast to str
    """

    def __init__(self, payload: dict, gelbooru):
        self._gelbooru = gelbooru  # type: Gelbooru

        self.id         = int(payload.get('id'))
        self.name       = payload.get('name')
        self.count      = int(payload.get('@count', 0))
        self.ambiguous  = payload.get('ambiguous')

    def __str__(self):
        return self.name

    def __int__(self):
        return self.id

    def __repr__(self):
        rep = reprlib.Repr()
        return f"<GelbooruTag(id={self.id}, name={rep.repr(self.name)}, count={self.count})>"


class GelbooruComment:
    """
    Container for Gelbooru post comments.
    Returns the comment itself when cast to str
    """

    def __init__(self, payload: dict, gelbooru, post: Optional[GelbooruImage] = None):
        self._gelbooru = gelbooru  # type: Gelbooru
        self._post = post

        self.id         = int(payload.get('id'))
        self.post_id    = payload.get('post_id')
        self.creator    = payload.get('creator')
        self.creator_id = payload.get('creator_id')
        self.created_at = payload.get('created_at')
        self.body       = payload.get('body')

    async def get_post(self):
        if self._post:
            return self._post

        self._post = await self._gelbooru.get_post(self.post_id)
        return self._post

    def __str__(self):
        return self.body

    def __int__(self):
        return self.id

    def __repr__(self):
        rep = reprlib.Repr()
        return f"<GelbooruComment(id={self.id}, author={rep.repr(self.creator)}, created_at={self.created_at})>"


class Gelbooru:
    BASE_URL = 'https://gelbooru.com/'

    SORT_COUNT = 'count'
    SORT_DATE = 'date'
    SORT_NAME = 'name'

    SORT_ASC = 'ASC'
    SORT_DESC = 'DESC'

    def __init__(self, api_key: Optional[str] = None,
                 user_id: Optional[str] = None,
                 loop: Optional[asyncio.AbstractEventLoop] = None):
        """
        API credentials can be obtained here (registration required):
        https://gelbooru.com/index.php?page=account&s=options
        Args:
            api_key (str): API Key
            user_id (str): User ID
        """
        self._api_key = api_key
        self._user_id = user_id
        self._loop = None

    async def get_post(self, post_id: int) -> Optional[GelbooruImage]:
        """
        Get a specific Gelbooru post by its ID
        Args:
            post_id (int): The post id to lookup
        Raises:
            GelbooruNotFoundException: Raised if Gelbooru returns an empty result for this query
        """
        endpoint = self._endpoint('post')
        endpoint.args['id'] = post_id

        # Fetch and parse XML, then make sure we actually have results
        payload = await self._request(str(endpoint))
        payload = xmltodict.parse(payload)
        if 'posts' not in payload:
            raise GelbooruNotFoundException(f"Could not find a post with the ID {post_id}")

        return GelbooruImage(payload['posts']['post'], self)

    async def random_post(self, *, tags: Optional[List[str]] = None,
                          exclude_tags: Optional[List[str]] = None) -> Optional[List[GelbooruImage]]:
        """
        Search for and return a single random image with the specified tags.
        Args:
            tags (list of str): A list of tags to search for
            exclude_tags (list of str): A list of tags to EXCLUDE from search results
        Returns:
            GelbooruImage or None: Returns None if no posts are found with the specified tags.
        """
        endpoint = self._endpoint('post')
        endpoint.args['limit'] = 1

        # Apply basic tag formatting
        tags = self._format_tags(tags, exclude_tags)
        if tags:
            endpoint.args['tags'] = ' '.join(tags)

        # Run the initial query to get the number of posts available
        payload = await self._request(str(endpoint))
        try:
            payload = xmltodict.parse(payload)
        except xml.parsers.expat.ExpatError:
            raise GelbooruException("Gelbooru returned a malformed response")

        # Count is 0? We have no results to fetch then
        count = int(payload['posts']['@count'])
        if not count:
            return None

        # Otherwise, let's pull a random ID from the number of posts
        offset = randint(0, min(count, 20000))

        return await self.search_posts(tags=tags, exclude_tags=exclude_tags, limit=1, page=offset)

    async def search_posts(self, *, tags: Optional[List[str]] = None,
                           exclude_tags: Optional[List[str]] = None,
                           limit: int = 100,
                           page: int = 0) -> Union[List[GelbooruImage], GelbooruImage]:
        """
        Search for images with the optionally specified tag(s)
        Args:
            tags (list of str): A list of tags to search for
            exclude_tags (list of str): A list of tags to EXCLUDE from search results
            limit (int): Limit the number of results returned. Defaults to 100
            page (int): The page number
        Returns:
            list of GelbooruImage or GelbooruImage: Returns a single GelbooruImage of a limit of 1 is supplied
        """
        endpoint = self._endpoint('post')
        endpoint.args['limit'] = limit
        endpoint.args['pid'] = page

        # Apply basic tag formatting
        tags = self._format_tags(tags, exclude_tags)
        if tags:
            endpoint.args['tags'] = ' '.join(tags)

        # Fetch and parse XML, then make sure we actually have results
        payload = await self._request(str(endpoint))
        try:
            payload = xmltodict.parse(payload)
        except xml.parsers.expat.ExpatError:
            raise GelbooruException("Gelbooru returned a malformed response")
        if 'posts' not in payload:
            return []
        elif 'post' not in payload["posts"]:
            return []

        # Single results are not returned as arrays/lists and need to be processed directly instead of iterated
        result = [GelbooruImage(p, self) for p in payload['posts']['post']] \
            if isinstance(payload['posts']['post'], list) \
            else [GelbooruImage(payload['posts']['post'], self)]

        # Return the first result if we have a limit of 1 explicitly set
        if limit == 1:
            return result[0]
        else:
            return result

    async def tag_list(self, *, name: Union[str, List[str], None] = None,
                       name_pattern: Optional[str] = None,
                       limit: int = 100,
                       sort_by: str = SORT_COUNT,
                       sort_order: str = SORT_DESC) -> Union[GelbooruTag, List[GelbooruTag]]:
        """
        Get a list of tags, optionally filtered and sorted as needed
        Args:
            name (str or list of str): A single tag name to query or a list of tags
            name_pattern (str): A wildcard search for your query using LIKE. (choolgirl would act as *choolgirl* wildcard search.) Cannot be used with names.
            limit (int): Limit the number of results returned. Defaults to 100
            sort_by (): Sort by either SORT_COUNT (tag usage count), SORT_NAME, or SORT_DATE
            sort_order (): Sort order; either SORT_ASC or SORT_DESC
        Returns:
            list of GelbooruTag or GelbooruTag: Returns the first result if querying a single tag
        """
        endpoint = self._endpoint('tag')
        endpoint.args['limit'] = limit

        # Name filtering
        if name:
            if isinstance(name, list):
                endpoint.args['names'] = ' '.join([n.strip().lower().replace(' ', '_') for n in name])
            else:
                endpoint.args['name'] = name.strip().lower().replace(' ', '_')
        elif name_pattern:
            endpoint.args['name_pattern'] = name_pattern.strip().lower().replace(' ', '_')

        # Sorting
        endpoint.args['orderby'] = sort_by
        endpoint.args['order'] = sort_order

        # Fetch and parse XML, then make sure we actually have results
        payload = await self._request(str(endpoint))
        payload = xmltodict.parse(payload)
        if 'tags' not in payload:
            return []

        # Single results are not returned as arrays/lists and need to be processed directly instead of iterated
        return [GelbooruTag(t, self) for t in payload['tags']['tag']] \
            if isinstance(payload['tags']['tag'], list) \
            else GelbooruTag(payload['tags']['tag'], self)

    async def get_comments(self, post: Union[int, GelbooruImage]) -> List[GelbooruComment]:
        """
        Get comments for the specified post ID
        Args:
            post (int): The Gelbooru post id
        Returns:
            list of GelbooruComment
        """
        endpoint = self._endpoint('comment')
        endpoint.args['post_id'] = int(post)

        # Fetch and parse XML, then make sure we actually have results
        payload = await self._request(str(endpoint))
        payload = xmltodict.parse(payload)
        if 'comment' not in payload['comments']:
            return []

        # Single results are not returned as arrays/lists and need to be processed directly instead of iterated
        post = post if isinstance(post, GelbooruImage) else None  # Enables backreferencing without additional queries
        return [GelbooruComment(c, self, post) for c in payload['comments']['comment']] \
            if isinstance(payload['comments']['comment'], list) \
            else [GelbooruComment(payload['comments']['comment'], self, post)]

    async def is_deleted(self, image_md5: str) -> bool:
        """
        Check if an image has been deleted from Gelbooru
        Args:
            image_md5 (str): The md5 hash of the image to check
        Returns:
            bool
        """
        endpoint = self._endpoint('post')
        endpoint.args['deleted'] = 'show'

        # Fetch and parse XML
        payload = await self._request(str(endpoint))
        payload = xmltodict.parse(payload)

        deleted_md5s = [p['@md5'] for p in payload['posts']['post']]
        return image_md5 in deleted_md5s

    def _endpoint(self, s: str) -> furl:
        endpoint = furl(self.BASE_URL)
        endpoint.args['page'] = 'dapi'
        endpoint.args['s'] = s
        endpoint.args['q'] = 'index'
        # endpoint.args['json'] = '1'

        # Append API key if available
        if self._api_key:
            endpoint.args['api_key'] = self._api_key
        if self._user_id:
            endpoint.args['user_id'] = self._user_id

        return endpoint

    def _format_tags(self, tags: list, exclude_tags: list):
        """
        Apply basic tag formatting
        Args:
            tags (list of str): A list of tags to search for
            exclude_tags (list of str): A list of tags to EXCLUDE from search results
        Returns:
            list of str
        """
        # Apply basic tag formatting
        tags = [tag.strip().lower().replace(' ', '_') for tag in tags] if tags else []
        exclude_tags = ['-' + tag.strip().lstrip('-').lower().replace(' ', '_') for tag in
                        exclude_tags] if exclude_tags else []

        return tags + exclude_tags

    async def _request(self, url: str) -> bytes:
        async with aiohttp.ClientSession(loop=self._loop) as session:
            status_code, response = await self._fetch(session, url)

        if status_code not in [200, 201]:
            raise GelbooruException(f"""Gelbooru returned a non 200 status code: {response}""")

        return response

    async def _fetch(self, session: aiohttp.ClientSession, url) -> Tuple[int, bytes]:
        async with session.get(url) as response:
            return response.status, await response.read()
