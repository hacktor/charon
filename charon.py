#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
Charon - Ferry toots and tweets between Mastodon and Twitter
"""

from threading import Thread, Lock
import argparse
from collections import namedtuple
import html
import yaml
import logging
import os.path
from time import sleep
import urllib
import tempfile
from pprint import pprint

from bs4 import BeautifulSoup
from mastodon import Mastodon, StreamListener, MastodonMalformedEventError, MastodonNetworkError
import twitter as Twitter


# Get CLI argments and parse them
parser = argparse.ArgumentParser(description="Charon, ferry bot between Mastodon and Twitter")
parser.add_argument("--config", dest="config", metavar="<YAML file>", default="config.yaml", type=str, help="YAML file to use as config")
parser.add_argument("--debug", "-d", dest="debug", action="store_true", help="enable debugging output")
args = parser.parse_args()

# Configure logging
logger = logging.getLogger("Charon")
logger.setLevel(logging.DEBUG)
ch = logging.StreamHandler()
ch.setLevel(logging.INFO)
if args.debug:
    ch.setLevel(logging.DEBUG)
formatter = logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s')
ch.setFormatter(formatter)
logger.addHandler(ch)

# Read config from YAML config file
if not os.path.exists(args.config):
    exit("{} does not exist".format(args.config))
config = None
with open(args.config, "r") as fh:
    config = yaml.safe_load(fh)

# Set Twitter maximum characters
TWITTER_CHARS = 280

# Set up Mastodon API client
mastodon = Mastodon(
    client_id = config['mastodon']['client_key'],
    client_secret = config['mastodon']['client_secret'],
    access_token = config['mastodon']['access_token'],
    api_base_url = config['mastodon']['api_base_url']
)

# Set up Twitter API client
twitter = Twitter.Api(
    consumer_key = config['twitter']['consumer_key'],
    consumer_secret = config['twitter']['consumer_secret'],
    access_token_key = config['twitter']['access_token'],
    access_token_secret = config['twitter']['access_token_secret'],
    sleep_on_rate_limit=True 
)

# Connect to Mastodon and Twitter with credentials
# (also useful for checking the connection)
mastodon_user = mastodon.account_verify_credentials()
twitter_user = twitter.VerifyCredentials()


class LastId:
    """ Shared class for remembering last ID's """
    lasttweet = None
    tweetlock = Lock()

    def get_lasttweet():
        with LastId.tweetlock:
            return LastId.lasttweet

    def set_lasttweet(t):
        with LastId.tweetlock:
            LastId.lasttweet = t


class StatusReceiver(StreamListener):
    """StreamListener child class with handlers for received stream events.
    """
    def __init__(self):
        super().__init__()
        logger.info("StatusReceiver for %s initialised" % mastodon_user.url)

    def on_update(self, toot):
        # Filter out certain Toots
        logger.info(toot)

        # Don't handle Toots by application
        if toot.application.name == config['mastodon']['app_name']:
            logger.info("Own Toot was filtered out; twitter application")
            return

        # Don't handle Toots from other people
        if toot.account.id != mastodon_user.id and toot.account.username != mastodon_user.username:
            logger.info("Received Toot from other account, ignored")
            return

        # Skip private Toots
        if toot.visibility not in ['public', 'unlisted']:
            logger.info("Own Toot was filtered out due to visibility filter")
            return

        # Skip replies
        if toot.in_reply_to_id != None:
            logger.info("Own Toot was filtered out due to mention/in_reply_to_id filter")
            return

        # Skip reblogs/boosts
        if toot.reblog:
            logger.info("Own Toot was filtered out due to reblog filter")
            return

        # Temporary dir to place media when processing multimedia toots
        # the tempdir is only created when handling a multimedia toot
        media_tmp_dir = None

        # Unescape HTML entities in content
        # This means, that e.g. a Toot "Hi, I'm a bot" is escaped as
        # "Hi, I&apos;m a bot". Unescaping this text reconstructs the
        # original string "I'm"
        content = html.unescape(toot['content'])

        # Pass the resulting HTML to BeautifulSoup and extract all text
        content = BeautifulSoup(content, 'html.parser')
        if 'prefix' in config['twitter']:
            content = "{}: {}".format(config['twitter']['prefix'], content.get_text())
        else:
            content = content.get_text()

        # Filter @username@twitter.com mentions
        content = content.replace("@twitter.com", "")

        # Fetch the toot URL
        url = "{}".format(toot['url'][8:])

        # Take first x tags of post to add to the post
        # Uses tags_to_append form the config to count how many tags to use
        toot_tags = reversed(toot['tags'])  # Tags are sorted, but in reverse
        tag = ""
        for idx, item in enumerate(toot_tags):
            new_tag = item['name']
            tag += " #{}".format(new_tag)
            if idx+1 == config['tags_to_append']:
                break

        # Process the images in media_attachments (if any)
        tweet_media = []
        if config['post_media']:
            media_tmp_dir = tempfile.TemporaryDirectory()
            media_attachments = toot.media_attachments
            for media_file in media_attachments:
                media_id = media_file['id']
                media_type = media_file['type']
                media_url = media_file['url']
                media_content = urllib.request.urlopen(media_url)
                media_filename = "{0}/{1}.jpg".format(media_tmp_dir.name, media_id)
                if media_type == 'image':
                    with open(media_filename, 'wb') as f:
                        f.write(media_content.read())
                    tweet_media.append(media_filename)

        # Build the tweet
        tweets = []
        if config['link_to_mastodon']:
            # Append a link back to the Toot
            if len(content) + len(url) <= TWITTER_CHARS:
                # Content + URL are fitting in one Tweet
                tweet = "{content} {url}".format(content=content, url=url)
            else:
                # Calculate snipped of content plus all other text parts
                tweet = content[:TWITTER_CHARS - len(tag) - len(url) - 4]
                # Remove spaces between last word and "…"
                tweet = tweet.strip()
                tweet += "… {tag} {url}".format(tag=tag, url=url)
            tweets.append(tweet)
        else:
            # Don't link back to Mastodon
            if len(content) > TWITTER_CHARS:
                # The Toot has more characters than Twitter allows

                # Get the length, calculate the half and split the words
                length = len(content)
                middle = length/2
                tokens = content.split(" ")

                # Mastodon allows 500 characters, Twitter 280: two Tweets are enough
                first_tweet = ""
                second_tweet = ""
                for idx, token in enumerate(tokens):
                    # Test, if an additional word would exceed the first half
                    if len("{} {}".format(first_tweet, token)) > middle:
                        # Construct the second Tweet from the remaining words and stop
                        second_tweet = " ".join(tokens[idx:])
                        break
                    # If it still fits, append the word to the first Tweet
                    first_tweet = "{} {}".format(first_tweet, token)

                tweets = [first_tweet, second_tweet]
            else:
                tweet = "{content}".format(content=content)
                tweets.append(tweet)

        # Tweet
        logger.info("Tweeting: %s" % tweets)
        process_tweets(tweets, tweet_media)

        # cleanup multimedia tempdir (if exists)
        if media_tmp_dir:
            media_tmp_dir.cleanup()

    def on_notification(self, notification):
        logger.info("Received notification, ignored")

    def on_abort(self, err):
        logger.error("on_abort: %s" % err)

    def on_delete(self, status_id):
        logger.info("Received status deletion event for Toot %s, ignored" % status_id)

    def handle_heartbeat(self):
        logger.info("Received heartbeat")


def process_tweets(tweets, tweet_media):
    """Post one or multiple Tweets to twitter.com
    """
    # To be used for threads with two or more related Tweets
    latest_id = None

    for tweet in tweets:
        try:
            # Post single Tweet
            status = twitter.PostUpdate(
                tweet,
                in_reply_to_status_id=latest_id,
                auto_populate_reply_metadata=False,
                media=tweet_media
            )
            # and save the Tweet ID
            latest_id = status.id
            # only the first Tweet contains the images from original toot
            tweet_media = ""
        except Twitter.error.TwitterError as ex:
            # Something went wrong while posting to Twitter, abort program
            logger.critical("Error posting tweet: '{tweet}' (length {length}). {error}"\
                .format(tweet=tweet, length=len(tweet), error=ex))
            exit(1)
        logger.debug(status)
        logger.info("Tweeted: {}".format(tweet.encode('utf-8')))


def twitterpoll():
    while 1:
        try:
            logger.info("Polling twitter for user {}".format(twitter_user.screen_name))
            for status in twitter.GetUserTimeline(
                    screen_name=twitter_user.screen_name,
                    exclude_replies=True,
                    count=1
                ):

                lasttweet = LastId.get_lasttweet()
                if not lasttweet:
                    logger.info(f"Skipping last known twitter status id {status.id}")
                    LastId.set_lasttweet(status.id)
                elif 'prefix' in config['twitter'] and config['twitter']['prefix'] in status.text:
                    logger.debug("Skipping Mastodon source")
                elif status.id > lasttweet:
                    logger.debug(f"Tweet Text: {status.text}")
                    LastId.set_lasttweet(status.id)
                    toot = f"{status.text}\n"
                    if config['link_to_twitter']:
                        toot += f"\nhttps://twitter.com/{status.user.screen_name}/status/{status.id}"
                    mastodon.toot(toot)
                    logger.info("TOOT status.id")
                else:
                    logger.debug(f"Found tweet ID: {status.id}; LAST_ID: {lasttweet}, skipping")
        except Exception as ex:
            logger.error("Catched unknown Exception in Twitterpoll: %s" % ex)
        sleep(30)

# Start twitterpoll as a thread
Thread(target=twitterpoll).start()


# Handle exceptions thrown by the StreamListener
# and restart StatusReceiver if an exception occurred
while 1:
    try:
        logger.debug("Creating new StatusReceiver instance")
        receiver = StatusReceiver()
        logger.debug("Starting stream_user with StatusReceiver")
        mastodon.stream_user(receiver, run_async=False)
    except MastodonMalformedEventError as ex:
        logger.error("Catched MastodonMalformedEventError: %s" % ex)
    except MastodonNetworkError as ex:
        logger.error("Catched MastodonNetworkError: %s" % ex)
    except Exception as ex:
        logger.error("Catched unknown StreamListener Exception: %s" % ex)
