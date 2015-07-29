import logging
import sys

from errbot.backends.base import RoomError
from errbot.errBot import ErrBot
from errbot.rendering import text


# Can't use __name__ because of Yapsy
log = logging.getLogger('errbot.backends.telegram')

try:
    import telegram
except ImportError:
    log.exception("Could not start the Telegram back-end")
    log.fatal(
        "You need to install the python-telegram-bot package in order "
        "to use the Telegram back-end. "
        "You should be able to install this package using: "
        "pip install python-telegram-bot"
    )
    sys.exit(1)


class RoomsNotSupportedError(RoomError):
    def __init__(self, message=None):
        if message is None:
            message = (
                "Room operations are not supported on Telegram. "
                "While Telegram itself has groupchat functionality, it does not "
                "expose any APIs to bots to get group membership or otherwise "
                "interact with groupchats."
            )
        super().__init__(message)


class TelegramBotFilter(object):
    """
    This is a filter for the logging library that filters the
    "No new updates found." log message generated by telegram.bot.

    This is an INFO-level log message that gets logged for every
    getUpdates() call where there are no new messages, so is way
    too verbose.
    """

    @staticmethod
    def filter(record):
        if record.getMessage() == "No new updates found.":
            return 0


class TelegramIdentifier(object):

    def __init__(self, id, first_name=None, last_name=None, username=None, title=None):
        self._id = id
        self._first_name = first_name
        self._last_name = last_name
        self._username = username
        self._title = title

    def __unicode__(self):
        return str(self._id)

    __str__ = __unicode__

    @property
    def id(self):
        return self._id

    @property
    def first_name(self):
        return self._first_name

    @property
    def last_name(self):
        return self._last_name

    @property
    def fullname(self):
        fullname = self.first_name
        if self.last_name is not None:
            fullname += " " + self.last_name
        return fullname

    @property
    def username(self):
        return self._username

    @property
    def client(self):
        return None

    @property
    def title(self):
        """Return the groupchat title (only applies to groupchats)"""
        return self._title

    person = id
    nick = username


class TelegramMUCOccupant(TelegramIdentifier):
    """
    This class represents a person inside a MUC.
    """
    room = TelegramIdentifier.id


class TelegramBackend(ErrBot):

    def __init__(self, config):
        super().__init__(config)
        logging.getLogger('telegram.bot').addFilter(TelegramBotFilter())

        identity = config.BOT_IDENTITY
        self.token = identity.get('token', None)
        if not self.token:
            log.fatal(
                "You need to supply a token for me to use. You can obtain "
                "a token by registering your bot with the Bot Father (@BotFather)"
            )
            sys.exit(1)
        self.telegram = None  # Will be initialized in serve_once
        self.bot_instance = None  # Will be set in serve_once
        self.md_converter = text()

    def serve_once(self):
        log.info("Initializing connection")
        try:
            self.telegram = telegram.Bot(token=self.token)
            me = self.telegram.getMe()
        except telegram.TelegramError as e:
            log.error("Connection failure: %s", e.message)
            return False

        self.bot_identifier = TelegramIdentifier(
            id=me.id,
            first_name=me.first_name,
            last_name=me.last_name,
            username=me.username
        )

        log.info("Connected")
        self.reset_reconnection_count()
        self.connect_callback()

        try:
            offset = 0
            while True:
                log.debug("Getting updates with offset %s", offset)
                for update in self.telegram.getUpdates(offset=offset, timeout=60):
                    offset = update.update_id + 1
                    log.debug("Processing update: %s", update)
                    if not hasattr(update, 'message'):
                        log.warning("Unknown update type (no message present)")
                        continue
                    try:
                        self._handle_message(update.message)
                    except Exception:
                        log.exception("An exception occurred while processing update")
                log.debug("All updates processed, new offset is %s", offset)
        except KeyboardInterrupt:
            log.info("Interrupt received, shutting down..")
            return True
        except:
            log.exception("Error reading from Telegram updates stream:")
        finally:
            log.debug("Triggering disconnect callback")
            self.disconnect_callback()

    def _handle_message(self, message):
        """
        Handle a received message.

        :param message:
            A message with a structure as defined at
            https://core.telegram.org/bots/api#message
        """
        if message.text is None:
            log.warning("Unhandled message type (not a text message) ignored")
            return

        message_instance = self.build_message(message.text)
        if isinstance(message.chat, telegram.user.User):
            message_instance.type = "chat"
            message_instance.frm = TelegramIdentifier(
                id=message.from_user.id,
                first_name=message.from_user.first_name,
                last_name=message.from_user.last_name,
                username=message.from_user.username
            )
            message_instance.to = self.bot_identifier
        else:
            message_instance.type = "groupchat"
            message_instance.frm = TelegramMUCOccupant(
                id=message.from_user.id,
                first_name=message.from_user.first_name,
                last_name=message.from_user.last_name,
                username=message.from_user.username
            )
            message_instance.to = TelegramIdentifier(
                id=message.chat.id,
                title=message.chat.title
            )

        self.callback_message(message_instance)

    def send_message(self, mess):
        super().send_message(mess)
        body = self.md_converter.convert(mess.body)
        try:
            self.telegram.sendMessage(mess.to.id, body)
        except Exception:
            log.exception(
                "An exception occurred while trying to send the following message "
                "to %s: %s" % (mess.to.id, mess.body)
            )
            raise

    def build_identifier(self, txtrep):
        """
        Convert a textual representation into a :class:`~TelegramIdentifier`.
        """
        log.debug("building an identifier from %s" % txtrep)
        id = txtrep.strip()
        if not self._is_numeric(id):
            raise ValueError("Telegram identifiers must be numeric")
        return TelegramIdentifier(id=id)

    def build_reply(self, mess, text=None, private=False):
        msg_type = mess.type
        response = self.build_message(text)

        response.frm = self.bot_identifier
        if private:
            response.type = 'chat'
            response.to = mess.frm
        else:
            response.type = msg_type
            response.to = mess.frm if mess.type == 'chat' else mess.to

        return response

    @property
    def mode(self):
        return 'telegram'

    def query_room(self, room):
        """
        Not supported on Telegram.

        :raises: :class:`~RoomsNotSupportedError`
        """
        raise RoomsNotSupportedError()

    def rooms(self):
        """
        Not supported on Telegram.

        :raises: :class:`~RoomsNotSupportedError`
        """
        raise RoomsNotSupportedError()

    def prefix_groupchat_reply(self, message, identifier):
        message.body = '@{0}: {1}'.format(identifier.nick, message.body)

    @staticmethod
    def _is_numeric(input):
        """Return true if input is a number"""
        try:
            int(input)
            return True
        except ValueError:
            return False
