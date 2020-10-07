"""Browse media for forked-daapd."""
from collections import namedtuple
from html import escape, unescape

from homeassistant.components.media_player import BrowseMedia
from homeassistant.components.media_player.const import (
    MEDIA_CLASS_ALBUM,
    MEDIA_CLASS_ARTIST,
    MEDIA_CLASS_DIRECTORY,
    MEDIA_CLASS_GENRE,
    MEDIA_CLASS_PLAYLIST,
    MEDIA_CLASS_TRACK,
    MEDIA_TYPE_ALBUM,
    MEDIA_TYPE_ARTIST,
    MEDIA_TYPE_GENRE,
    MEDIA_TYPE_PLAYLIST,
    MEDIA_TYPE_TRACK,
)
from homeassistant.components.media_player.errors import BrowseError

from .const import CAN_PLAY_TYPE

MEDIA_TYPE_DIRECTORY = "directory"

TOP_LEVEL_LIBRARY = {
    "Albums": (MEDIA_TYPE_ALBUM, ""),
    "Artists": (MEDIA_TYPE_ARTIST, ""),
    "Playlists": (MEDIA_TYPE_PLAYLIST, ""),
    "Albums by Genre": (MEDIA_TYPE_GENRE, MEDIA_TYPE_ALBUM),
    "Tracks by Genre": (MEDIA_TYPE_GENRE, MEDIA_TYPE_TRACK),
    "Genre": (MEDIA_TYPE_GENRE, MEDIA_TYPE_GENRE),
    "Directories": (MEDIA_TYPE_DIRECTORY, ""),
}
MEDIA_TYPE_TO_MEDIA_CLASS = {
    MEDIA_TYPE_ALBUM: MEDIA_CLASS_ALBUM,
    MEDIA_TYPE_ARTIST: MEDIA_CLASS_ARTIST,
    MEDIA_TYPE_TRACK: MEDIA_CLASS_TRACK,
    MEDIA_TYPE_PLAYLIST: MEDIA_CLASS_PLAYLIST,
    MEDIA_TYPE_GENRE: MEDIA_CLASS_GENRE,
    MEDIA_TYPE_DIRECTORY: MEDIA_CLASS_DIRECTORY,
}
CAN_EXPAND_TYPE = {
    MEDIA_TYPE_ALBUM,
    MEDIA_TYPE_ARTIST,
    MEDIA_TYPE_PLAYLIST,
    MEDIA_TYPE_GENRE,
    MEDIA_TYPE_DIRECTORY,
}
URI_TYPE_TO_MEDIA_TYPE = {
    "track": MEDIA_TYPE_TRACK,
    "playlist": MEDIA_TYPE_PLAYLIST,
    "artist": MEDIA_TYPE_ARTIST,
    "album": MEDIA_TYPE_ALBUM,
    "genre": MEDIA_TYPE_GENRE,
    MEDIA_TYPE_DIRECTORY: MEDIA_TYPE_DIRECTORY,
}
MEDIA_TYPE_TO_TITLE = {
    MEDIA_TYPE_ALBUM: "Album",
    MEDIA_TYPE_ARTIST: "Artist",
    MEDIA_TYPE_TRACK: "Track",
    MEDIA_TYPE_PLAYLIST: "Playlist",
    MEDIA_TYPE_GENRE: "Genre",
}

# payload type:
#   media_content_id == Name & URI & optional subtype for genre
#   URI in format library:type:id (for directories, id is path)
#   media_content_type - type of item (mostly used to check if playable or can expand)
#   MediaContent.type may differ from media_content_type when media_content_type is a directory
#   MediaContent.type is used in our own branching, but media_content_type is used for determining playability
MediaContent = namedtuple("MediaContent", ["title", "type", "id", "subtype"])


async def build_item_response(api, payload):
    """Create response payload for search described by payload."""

    def parse_content_id(content_id: str) -> MediaContent:
        content_id = content_id.split("&")
        split_uri = content_id[1].split(":")
        subtype = content_id[-1]  # subtype only valid and used for genre
        return MediaContent(
            title=unescape(content_id[0]),  # escaped to allow for all names
            type=URI_TYPE_TO_MEDIA_TYPE[split_uri[1]],
            id=split_uri[2],
            subtype=subtype,
        )

    media_content_type = payload["media_content_type"]
    media_content_id = parse_content_id(payload["media_content_id"])
    result = None
    # Query API for next level
    if media_content_id.type == MEDIA_TYPE_DIRECTORY:
        # returns tracks, directories, and playlists
        directory_path = media_content_id.id
        if directory_path:
            result = await api.get_directory(directory=directory_path)
        else:
            result = await api.get_directory()
    else:
        if media_content_id.id == "":  # top level search
            if media_content_id.type == MEDIA_TYPE_ALBUM:
                result = await api.get_albums()  # list of albums with name, artist, uri
            elif media_content_id.type == MEDIA_TYPE_ARTIST:
                result = await api.get_artists()  # list of artists with name, uri
            elif media_content_id.type == MEDIA_TYPE_GENRE:
                result = await api.get_genres()  # returns list of genre names
                for item in result:  # add generated genre uris to list of genre names
                    # subtype is the desired result type
                    # escape fields which may include ampersands
                    item[
                        "uri"
                    ] = f"library:{MEDIA_TYPE_GENRE}:{escape(item['name'])}&{media_content_id.subtype}"
            elif media_content_id.type == MEDIA_TYPE_PLAYLIST:
                result = await api.get_playlists()  # list of playlists with name, uri
        else:  # we should have content type and id
            if media_content_id.type == MEDIA_TYPE_ALBUM:
                result = await api.get_tracks(album_id=media_content_id.id)
            elif media_content_id.type == MEDIA_TYPE_ARTIST:
                result = await api.get_albums(artist_id=media_content_id.id)
            elif media_content_id.type == MEDIA_TYPE_GENRE:
                if media_content_id.subtype == MEDIA_TYPE_ALBUM:
                    result = await api.get_genre(
                        unescape(media_content_id.id), media_type="albums"
                    )
                elif media_content_id.subtype == MEDIA_TYPE_TRACK:
                    result = await api.get_genre(
                        unescape(media_content_id.id), media_type="tracks"
                    )
                elif media_content_id.subtype == MEDIA_TYPE_GENRE:
                    result = await api.get_genre(unescape(media_content_id.id))
            elif media_content_id.type == MEDIA_TYPE_PLAYLIST:
                result = await api.get_tracks(playlist_id=media_content_id.id)

    if result is None:
        raise BrowseError(
            f"Media not found for {media_content_type} / {payload['media_content_id']}"
        )

    # Fill in children
    children = []
    if media_content_id.type == MEDIA_TYPE_DIRECTORY:
        for directory in result["directories"]:
            path = directory["path"]
            children.append(
                BrowseMedia(
                    title=path,
                    media_class=MEDIA_CLASS_DIRECTORY,
                    media_content_id=f"{path}&library:{MEDIA_TYPE_DIRECTORY}:{path}",
                    media_content_type=MEDIA_TYPE_DIRECTORY,
                    can_play=False,
                    can_expand=True,
                )
            )
        result = result["tracks"]["items"] + result["playlists"]["items"]

    for item in result:
        # uri in format of library:album:1234
        media_type = URI_TYPE_TO_MEDIA_TYPE[item["uri"].split(":")[1]]
        title = item.get("name") or item.get("title")  # only tracks use title
        children.append(
            BrowseMedia(
                title=title,
                media_class=MEDIA_TYPE_TO_MEDIA_CLASS[media_type],
                media_content_id=f"{MEDIA_TYPE_TO_TITLE[media_type]}/{title}&{item['uri']}",
                media_content_type=media_type,
                can_play=media_type in CAN_PLAY_TYPE,
                can_expand=media_type in CAN_EXPAND_TYPE,
                thumbnail=api.full_url(item["artwork_url"])
                if item.get("artwork_url")
                else None,
            )
        )

    return BrowseMedia(
        title=directory_path
        if media_content_id.type == MEDIA_TYPE_DIRECTORY
        else media_content_id.title,
        media_class=MEDIA_TYPE_TO_MEDIA_CLASS[media_content_type],
        media_content_id=payload["media_content_id"],
        media_content_type=media_content_type,
        can_play=media_content_type in CAN_PLAY_TYPE,
        can_expand=media_content_type in CAN_EXPAND_TYPE,
        children=children,
    )


def library_payload():
    """Create response payload to describe contents of library."""

    top_level_items = []
    for name, (media_type, media_subtype) in TOP_LEVEL_LIBRARY.items():
        top_level_items.append(
            BrowseMedia(
                title=name,
                media_class=MEDIA_CLASS_DIRECTORY,
                media_content_id=f"{name}&library:{media_type}:&{media_subtype}",
                media_content_type=MEDIA_TYPE_DIRECTORY,
                can_play=False,
                can_expand=True,
            )
        )

    return BrowseMedia(
        title="Music Library",
        media_class=MEDIA_CLASS_DIRECTORY,
        media_content_id="library",  # Can be None
        media_content_type="library",  # Can be None
        can_play=False,
        can_expand=True,
        children=top_level_items,
    )
