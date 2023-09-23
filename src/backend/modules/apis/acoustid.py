"""
This module provides functionality to access the AcoustID API (https://acoustid.org)
to identify a segment of song data. AcoustID will only recognise a segment if it is a full
song.

Requests and fingerprinting are performed using the ``acoustid`` library, so exceptions raised by
this module will use the exception classes from ``acoustid``.
"""

import os

import acoustid
import utils.list_helper
from modules.audio_stream_io import save_numpy_as_audio_file
from utils.logger import log_error

METADATA_ALL = ["tracks", "recordings", "releasegroups"]
"""The metadata to query from the AcoustID API.
    * "tracks" offers the track title.
    * "recordings" offers the track artist.
    * "releasegroups" offers the albums the track was published on.

  As of current, only three sets of metadata can be requested, if four or more are sent,
  some are discarded. If this limitation is ever removed, this should also add the "recordingids"
  metadata to make merging matching recordings easier.
"""

titles_identified_by_acoustid = []
"""A list of all titles that were identified by AcoustID.
This is used to prevent duplicate submissions to the AcoustID database.
"""


def create_fingerprint(song_data, samplerate):
    """Create a chromaprint/AcoustID fingerprint for the given audio data
    in order to identify it using AcoustID.
    As of current, this works by writing the data to a temporary file
    and using the fpcalc command line tool to generate the fingerprint.
    The temporary file is deleted immediately afterwards.

    TODO: If it becomes feasible to build and distribute DLL versions of chromaprint
    for all target platforms, this should be refactored to use that instead.

    :param song_data: the audio data to generate a fingerprint from.
    :param samplerate: the audio data's sample rate.
    :returns: (song_duration, fingerprint).
        ``song_duration`` is measured in seconds and used for the API call to AcoustID.
        ``fingerprint`` is generated by fpcalc.
    :raise acoustid.NoBackendError: if fpcalc is not installed.
    :raise acoustid.FingerprintGenerationError: if fingerprint generation fails.
    """
    filename = "TEMP_FILE_FOR_FINGERPRINTING"
    save_numpy_as_audio_file(song_data, os.path.abspath(filename), "", rate=samplerate)

    filename_with_path = os.path.abspath(filename + ".mp3")
    fingerprint_duration, fingerprint = acoustid.fingerprint_file(
        filename_with_path, force_fpcalc=True
    )
    os.remove(filename_with_path)
    return (fingerprint_duration, fingerprint)


def submit(file_name: str, metadata: dict, api_key: str, user_key: str):
    """Submit a fingerprint for the provided file to be added to the AcoustID database.

    If the song was previously identified using AcoustID, it isn't submitted. This is to avoid
    spamming the AcoustID servers with duplicate submissions.

    This uses the ``pyacoustid`` wrapper. All exceptions raised by ``pyacoustid`` are
    handled within this function and lead to returning "False".

    :param file_name: The name of the file to submit.
    :param metadata: The metadata of the song to submit, formatted as a dict.
    :param api_key: The application API key.
    :param user_key: The user API key.
    :returns: boolean indicating whether the submission was successful.
    """
    global timestamp_last_acoustid_request

    titles_identified_key = metadata["title"] + "_" + metadata["artist"]

    # avoid submitting titles that have been identified by acoustid - we don't want duplicates
    if titles_identified_key in titles_identified_by_acoustid:
        return False
    titles_identified_by_acoustid.append(titles_identified_key)

    try:
        duration, fingerprint = acoustid.fingerprint_file(file_name, force_fpcalc=True)
    except acoustid.FingerprintGenerationError as ex:
        log_error(ex, "AcoustID fingerprint generation error")
        return False

    query_params = {
        "duration": duration,
        "fingerprint": fingerprint,
        "track": metadata["title"] if "title" in metadata else None,
        "artist": metadata["artist"] if "artist" in metadata else None,
        "album": metadata["album"] if "album" in metadata else None,
        "albumartist": metadata["albumartist"] if "albumartist" in metadata else None,
        "year": metadata["year"] if "year" in metadata else None,
    }

    try:
        acoustid.submit(api_key, user_key, query_params)
        return True
    except acoustid.FingerprintSubmissionError as ex:
        log_error(ex, "AcoustID submission error")
    except acoustid.WebServiceError as ex:
        log_error(ex, "AcoustID submit error")
    return False


def lookup(fingerprint, fingerprint_duration, api_key):
    """Get data about the provided fingerprint from the AcoustID API.
    This uses the ``pyacoustid`` library to make the actual API call, but parsing
    it is handled through custom functions to allow retrieving more metadata.

    The following metadata can be retrieved:
        * artist
        * title
        * album
        * albumartist

    If a recording has at least one album without a secondary type (secondary types being
    compilations, film soundtracks, ...), all albums with secondary types are filtered
    out from the metadata options to avoid excessive clutter.

    :param fingerprint: the fingerprint generated using ``_create_fingerprint``.
    :param fingerprint_duration: duration of the fingerprinted data, in seconds.
    :returns: A ``list`` of ``dict`` s containing the results.
        Example::

            [
              {
                "title": "Thunderstruck",
                "artist": "AC/DC",
                "album": "The Razor's Edge",
                "albumartist": "AC/DC"
              },
              {
                "title": "Thunderstruck",
                "artist": "2Cellos"
              }
            ]
    :raise acoustid.WebServiceError: if the request fails.
    """
    return _parse_lookup_result(
        acoustid.lookup(
            api_key,
            fingerprint,
            fingerprint_duration,
            meta=METADATA_ALL,
        )
    )


def _parse_lookup_result(data):
    """This is an extended/altered version of acoustid.parse_lookup_result.
    Retrieve the song metadata from the data returned by an AcoustID API call.
    Results that do not contain recordings are discarded, as they aren't useful.

    If a recording has at least one album without a secondary type (secondary types being
    compilations, film soundtracks, ...), all albums with secondary types are filtered
    out from the metadata options to avoid excessive clutter.

    The following metadata can be retrieved:
        * title
        * artist
        * album
        * albumartist

    :param data: The parsed JSON response from acoustid.lookup().
    :returns: A ``list`` of ``dict`` s containing metadata.
    :raise acoustid.WebServiceError: if the response is incomplete or the request failed.
    """
    if data["status"] != "ok":
        raise acoustid.WebServiceError("status: %s" % data["status"])
    if "results" not in data:
        raise acoustid.WebServiceError("results not included")

    recordings = _extract_recordings(data["results"])
    return _get_results_for_recordings(recordings)


def _extract_recordings(results):
    """Extract all recordings from the results returned by AcoustID.

    If a recording has at least one album without a secondary type (secondary types being
    compilations, film soundtracks, ...), all albums with secondary types are filtered
    out from the metadata options to avoid excessive clutter.

    :param results: The "results" segment of the AcoustID response.
    :returns: a ``list`` of all recordings. Recordings with the same title and set of artists are
        merged and if a non-compilation releasegroup exists, all compilations are filtered out.
    """
    all_recordings = utils.list_helper.flatten(
        [result["recordings"] for result in results if "recordings" in result]
    )
    return _merge_matching_recordings(all_recordings)


def _merge_matching_recordings(recordings: list):
    """Merge recordings with the same title and artists.
    This iterates over all recordings and merges the "releasegroups" sections of ones with the same
    title and artists.

    If a recording has at least one album without a secondary type (secondary types being
    compilations, film soundtracks, ...), all albums with secondary types are filtered
    out from the metadata options to avoid excessive clutter.

    TODO: This should be refactored to be more pythonic and readable, if possible.

    :param recordings: a ``list`` of ``recording`` ``dict`` s as provided by the AcoustID API.
    :returns: a ``list`` of ``recordings`` where matching entries were merged.
    """
    grouped_by_title_and_artist = {}
    artists_by_title_and_artist = {}
    for recording in recordings:
        if "title" not in recording or "artists" not in recording:
            # no title or no artist => useless data, discard
            continue
        title = recording["title"]
        artist_id = ",".join(
            [artist.setdefault("id", "") for artist in recording["artists"]]
        )
        if title not in grouped_by_title_and_artist:
            grouped_by_title_and_artist[title] = {}
            artists_by_title_and_artist[title] = {}
        if artist_id not in grouped_by_title_and_artist[title]:
            grouped_by_title_and_artist[title][artist_id] = []
            artists_by_title_and_artist[title][artist_id] = recording["artists"]
        grouped_by_title_and_artist[title][artist_id] = (
            grouped_by_title_and_artist[title][artist_id] + recording["releasegroups"]
        )
    return [
        {
            "title": title,
            "artists": artists_by_title_and_artist[title][artist_id],
            "releasegroups": _filter_out_compilations_from_releasegroups(
                utils.list_helper.remove_duplicate_dicts(releasegroups)
            ),
        }
        for title, entries in grouped_by_title_and_artist.items()
        for artist_id, releasegroups in entries.items()
    ]


def _filter_out_compilations_from_releasegroups(releasegroups):
    """If there is at least one album without a secondary type (such as "compilation", "soundtrack",
    etc.) in ``releasegroups``, exclude all albums with one.
    Otherwise, return the unfiltered list of releasegroups.

    :param releasegroups: The detected release groups.
    :returns: The filtered list of release groups, or the unfiltered list if the filtered list would
        be empty.
    """
    filtered_releasegroups = [
        releasegroup
        for releasegroup in releasegroups
        if ("secondarytypes" not in releasegroup)
    ]
    return filtered_releasegroups if len(filtered_releasegroups) != 0 else releasegroups


def _get_results_for_recordings(recordings):
    """Go through all the given recordings, parse their metadata into a dict and append them to the
    results list. To return all possible results, go through each release group the recording is in
    and append a separate result, so releases on different albums are identified separately.

    Metadata that do not contain an artist or a title are discarded, as they are not useful.

    The following metadata can be retrieved:
        * artist
        * title
        * album
        * albumartist

    :param recordings: The recordings to parse
    :param results: The list to append the results to.
    :returns: The list with the appended results.
    """
    global titles_identified_by_acoustid
    results = []
    for recording in recordings:
        # Get the artist if available.
        if "artists" not in recording or "title" not in recording:
            continue
        artist_name = _join_artist_names(recording["artists"])

        titles_identified_key = recording["title"] + "_" + artist_name
        if titles_identified_key not in titles_identified_by_acoustid:
            titles_identified_by_acoustid.append(titles_identified_key)

        for releasegroup in recording["releasegroups"]:
            results.append(
                _get_result_for_releasegroup(
                    releasegroup, artist_name, recording["title"]
                )
            )
    return results


def _get_result_for_releasegroup(releasegroup, artist_name: str, title: str):
    """Convert the given release group with the given parameters into a ``dict`` containing
    the metadata.

    The ``dict`` will have the following keys:
      * artist
      * title
      * album
      * albumartist

    If no album artist is set, the albumartist field will be ``None`` instead.

    :param releasegroup: The release group.
    :param artist_name: The artist name.
    :param title: The title.
    :returns: The ``dict`` containing the parsed release group.
    """
    album_artist_name = (
        _join_artist_names(releasegroup["artists"])
        if "artists" in releasegroup
        else None
    )
    album_title = (
        releasegroup["title"]
        if "title" in releasegroup
        else (releasegroup["name"] if "name" in releasegroup else None)
    )
    return {
        "artist": artist_name,
        "title": title,
        "album": album_title,
        "albumartist": album_artist_name,
    }


def _join_artist_names(artists):
    """Join all artist names from the given artists list.

    :param artists: List containing all artists to join together.
    :returns: The artist names, joined together with "; " as a separator.
    """
    names = [artist["name"] for artist in artists]
    return "; ".join(names)
