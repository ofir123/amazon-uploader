#!/usr/local/bin/python3
import datetime
from collections import defaultdict
import os
import sys

import logbook
import babelfish
from guessit import guessit
from showsformatter import format_show
import subliminal
from subliminal.cache import region
from subliminal.cli import dirs, cache_file, MutexLock
from subliminal.subtitle import get_subtitle_path

from ..clouduploader import config
from ..clouduploader.uploader import upload_file

# Directories settings.
MEDIA_ROOT_PATH = '/mnt/vdb/plexdrive/gdrive_decrypted'
TEMP_PATH = '/tmp'
# A map between each language and its favorite subliminal providers (None for all providers).
PROVIDERS_MAP = {
    babelfish.Language('heb'): ['thewiz', 'subscenter'],
    babelfish.Language('eng'): None
}

# The monitor will look only at the latest X files (or all of them if RESULTS_LIMIT is None).
RESULTS_LIMIT = 300

SUBTITLES_EXTENSION = '.srt'
LANGUAGE_EXTENSIONS = ['.he', '.en']
LOG_FILE_PATH = '/var/log/subtitles_monitor.log'

logger = logbook.Logger(__name__)


def _get_log_handlers():
    """
    Initializes all relevant log handlers.

    :return: A list of log handlers.
    """
    return [
        logbook.NullHandler(),
        logbook.StreamHandler(sys.stdout, level=logbook.INFO, bubble=True),
        logbook.RotatingFileHandler(LOG_FILE_PATH, level=logbook.DEBUG, max_size=5 * 1024 * 1024, backup_count=1,
                                    bubble=True)
    ]


def configure_subtitles_cache():
    """
    Configure the subliminal cache settings.
    Should be called once when the program starts.
    """
    # Configure the subliminal cache.
    cache_dir = dirs.user_cache_dir
    if not os.path.exists(cache_dir):
        os.makedirs(cache_dir)
    cache_file_path = os.path.join(cache_dir, cache_file)
    region.configure('dogpile.cache.dbm', expiration_time=datetime.timedelta(days=30),
                     arguments={'filename': cache_file_path, 'lock_factory': MutexLock})


def find_file_subtitles(original_path, current_path, language):
    """
    Finds subtitles for the given video file path in the given language.
    Downloaded subtitles will be saved next to the video file.

    :param original_path: The original path of the video file to find subtitles to.
    :param current_path: The current video path (to save the subtitles file next to).
    :param language: The language to search for.
    :return: The subtitles file path, or None if a problem occurred.
    """
    logger.info('Searching {} subtitles for file: {}'.format(language.alpha3, original_path))
    try:
        subtitles_result = None
        # Get required video information.
        video = subliminal.Video.fromguess(current_path, guessit(original_path))
        # Try using providers specified by the user.
        providers = PROVIDERS_MAP.get(language)
        current_result = subliminal.download_best_subtitles(
            {video}, languages={language}, providers=providers).values()
        if current_result:
            current_result = list(current_result)[0]
            if current_result:
                subtitles_result = current_result[0]
        # Handle results.
        if not subtitles_result:
            logger.info('No subtitles were found. Moving on...')
        else:
            logger.info('Subtitles found! Saving files...')
            # Save subtitles alongside the video file (if they're not empty).
            if subtitles_result.content is None:
                logger.debug('Skipping subtitle {}: no content'.format(subtitles_result))
            else:
                subtitles_file_name = os.path.basename(get_subtitle_path(video.name, subtitles_result.language))
                subtitles_path = os.path.join(TEMP_PATH, subtitles_file_name)
                logger.info('Saving {} to: {}'.format(subtitles_result, subtitles_path))
                try:
                    open(subtitles_path, 'wb').write(subtitles_result.content)
                    logger.info('Uploading {}'.format(subtitles_path))
                    try:
                        upload_file(subtitles_path)
                    except Exception:
                        # Catch all exceptions so the script won't stop.
                        logger.exception('Failed to upload file: {}'.format(subtitles_path))
                except OSError:
                    logger.error('Failed to save subtitles in path: {}'.format(subtitles_path))
                return subtitles_path
        return None
    except ValueError:
        # Subliminal raises a ValueError if the given file is not a video file.
        logger.info('Not a video file. Moving on...')


def main():
    """
    Start going over the video files and search for missing subtitles.
    """
    with logbook.NestedSetup(_get_log_handlers()).applicationbound():
        logger.info('Subtitles Monitor started!')
        # Verify paths.
        if not os.path.isfile(config.ORIGINAL_NAMES_LOG):
            raise FileNotFoundError('Couldn\'t read original names file! Stopping...')
        if not os.path.isdir(MEDIA_ROOT_PATH):
            raise NotADirectoryError('Couldn\'t find media root directory! Stopping...')
        try:
            original_paths_list = []
            subtitles_map = defaultdict(int)
            # Set subliminal cache first.
            logger.debug('Setting subtitles cache...')
            configure_subtitles_cache()
            logger.info('Going over the original names file...')
            with open(config.ORIGINAL_NAMES_LOG, 'r', encoding='utf8') as original_names_file:
                line = original_names_file.readline()
                while line != '':
                    original_path = line.strip()
                    original_paths_list.insert(0, original_path)
                    if RESULTS_LIMIT and len(original_paths_list) > RESULTS_LIMIT:
                        original_paths_list.pop()
                    # Fetch next line.
                    line = original_names_file.readline()
            logger.info('Searching for subtitles for the {} newest videos...'.format(RESULTS_LIMIT))
            for original_path in original_paths_list:
                # Create current path from original path.
                guess = guessit(original_path)
                extension = os.path.splitext(original_path)[1]
                title = guess.get('title')
                episode = guess.get('episode')
                if episode:
                    # Handle TV episodes.
                    season = guess.get('season')
                    base_dir = os.path.join(MEDIA_ROOT_PATH, config.CLOUD_TV_PATH)
                    # Translate show title if possible.
                    title = format_show(title)
                    current_path = os.path.join(base_dir, title, 'Season {:02}'.format(
                        season), '{} - S{:02}E{:02}{}'.format(title, season, episode, extension))
                else:
                    # Handle movies.
                    title = title.title()
                    year = guess.get('year')
                    base_dir = os.path.join(MEDIA_ROOT_PATH, config.CLOUD_MOVIE_PATH)
                    current_path = os.path.join(base_dir, '{} ({})'.format(title, year), '{} ({}){}'.format(
                        title, year, extension))
                # Check actual video file.
                if os.path.isfile(current_path):
                    logger.info('Checking subtitles for: {}'.format(current_path))
                    # Find missing subtitle files.
                    video_base_path = os.path.splitext(current_path)[0]
                    languages_list = []
                    for language_extension in LANGUAGE_EXTENSIONS:
                        if not os.path.isfile(video_base_path + language_extension + SUBTITLES_EXTENSION):
                            languages_list.append(babelfish.Language.fromalpha2(language_extension.lstrip('.')))
                    # Download missing subtitles.
                    for language in languages_list:
                        result_path = find_file_subtitles(original_path, current_path, language)
                        if result_path:
                            subtitles_map[language.alpha3] += 1
                else:
                    logger.info('Couldn\'t find: {}'.format(current_path))
            logger.info('All done! The results are: {}'.format(
                ', '.join(['{} - {}'.format(language, counter) for language, counter in subtitles_map.items()])))
        except:
            logger.exception('Critical exception occurred!')
            raise


if __name__ == '__main__':
    main()