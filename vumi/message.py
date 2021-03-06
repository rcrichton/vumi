# -*- test-case-name: vumi.tests.test_message -*-

import json
from uuid import uuid4
from datetime import datetime

from errors import MissingMessageField, InvalidMessageField

from vumi.utils import to_kwargs


# This is the date format we work with internally
VUMI_DATE_FORMAT = "%Y-%m-%d %H:%M:%S.%f"


def date_time_decoder(json_object):
    for key, value in json_object.items():
        try:
            json_object[key] = datetime.strptime(value,
                    VUMI_DATE_FORMAT)
        except ValueError:
            continue
        except TypeError:
            continue
    return json_object


class JSONMessageEncoder(json.JSONEncoder):
    """A JSON encoder that is able to serialize datetime"""
    def default(self, obj):
        if isinstance(obj, datetime):
            return obj.strftime(VUMI_DATE_FORMAT)
        return super(JSONMessageEncoder, self).default(obj)


def from_json(json_string):
    return json.loads(json_string, object_hook=date_time_decoder)


def to_json(obj):
    return json.dumps(obj, cls=JSONMessageEncoder)


class Message(object):
    """
    Start of a somewhat unified message object to be
    used internally in Vumi and while being in transit
    over AMQP

    scary transport format -> Vumi Tansport -> Unified Message -> Vumi Worker

    """

    def __init__(self, _process_fields=True, **kwargs):
        if _process_fields:
            kwargs = self.process_fields(kwargs)
        self.payload = kwargs
        self.validate_fields()

    def process_fields(self, fields):
        return fields

    def validate_fields(self):
        pass

    def assert_field_present(self, *fields):
        for field in fields:
            if field not in self.payload:
                raise MissingMessageField(field)

    def assert_field_value(self, field, *values):
        self.assert_field_present(field)
        if self.payload[field] not in values:
            raise InvalidMessageField(field)

    def to_json(self):
        return to_json(self.payload)

    @classmethod
    def from_json(cls, json_string):
        return cls(_process_fields=False, **to_kwargs(from_json(json_string)))

    def __str__(self):
        return u"<Message payload=\"%s\">" % repr(self.payload)

    def __repr__(self):
        return str(self)

    def __eq__(self, other):
        if isinstance(other, Message):
            return self.payload == other.payload
        return False

    def __contains__(self, key):
        return key in self.payload

    def __getitem__(self, key):
        return self.payload[key]

    def __setitem__(self, key, value):
        self.payload[key] = value

    def get(self, key, default=None):
        return self.payload.get(key, default)

    def items(self):
        return self.payload.items()

    def copy(self):
        return self.from_json(self.to_json())


class TransportMessage(Message):
    """Common base class for messages sent to or from a transport."""

    # sub-classes should set the message type
    MESSAGE_TYPE = None
    MESSAGE_VERSION = '20110921'
    DEFAULT_ENDPOINT_NAME = 'default'

    @staticmethod
    def generate_id():
        """
        Generate a unique message id.

        There are places where we want a message id before we can
        build a complete message. This lets us do that in a consistent
        manner.
        """
        return uuid4().get_hex()

    def process_fields(self, fields):
        fields.setdefault('message_version', self.MESSAGE_VERSION)
        fields.setdefault('message_type', self.MESSAGE_TYPE)
        fields.setdefault('timestamp', datetime.utcnow())
        fields.setdefault('routing_metadata', {})
        return fields

    def validate_fields(self):
        self.assert_field_value('message_version', self.MESSAGE_VERSION)
        self.assert_field_present(
            'message_type',
            'timestamp',
            )
        if self['message_type'] is None:
            raise InvalidMessageField('message_type')

    @property
    def routing_metadata(self):
        return self.payload.setdefault('routing_metadata', {})

    @classmethod
    def check_routing_endpoint(cls, endpoint_name):
        if endpoint_name is None:
            return cls.DEFAULT_ENDPOINT_NAME
        return endpoint_name

    def set_routing_endpoint(self, endpoint_name=None):
        endpoint_name = self.check_routing_endpoint(endpoint_name)
        self.routing_metadata['endpoint_name'] = endpoint_name

    def get_routing_endpoint(self):
        endpoint_name = self.routing_metadata.get('endpoint_name')
        return self.check_routing_endpoint(endpoint_name)


class TransportUserMessage(TransportMessage):
    """Message to or from a user.

    transport_type = sms, ussd, etc
    helper_metadata = for use by dispathers and off-to-the-side
                      components like failure workers (not for use
                      by transports or message workers).
    """

    MESSAGE_TYPE = 'user_message'

    # session event constants
    #
    # SESSION_NONE, SESSION_NEW, SESSION_RESUME, and SESSION_CLOSE
    # may be sent from the transport to a worker. SESSION_NONE indicates
    # there is no relevant session for this message.
    #
    # SESSION_NONE and SESSION_CLOSE may be sent from the worker to
    # the transport. SESSION_NONE indicates any existing session
    # should be continued. SESSION_CLOSE indicates that any existing
    # session should be terminated after sending the message.
    SESSION_NONE, SESSION_NEW, SESSION_RESUME, SESSION_CLOSE = (
        None, 'new', 'resume', 'close')

    # list of valid session events
    SESSION_EVENTS = frozenset([SESSION_NONE, SESSION_NEW, SESSION_RESUME,
                                SESSION_CLOSE])

    # canonical transport types
    TT_HTTP_API = 'http_api'
    TT_IRC = 'irc'
    TT_TELNET = 'telnet'
    TT_TWITTER = 'twitter'
    TT_SMS = 'sms'
    TT_USSD = 'ussd'
    TT_XMPP = 'xmpp'
    TT_MXIT = 'mxit'
    TRANSPORT_TYPES = set([TT_HTTP_API, TT_IRC, TT_TELNET, TT_TWITTER, TT_SMS,
                           TT_USSD, TT_XMPP, TT_MXIT])

    def process_fields(self, fields):
        fields = super(TransportUserMessage, self).process_fields(fields)
        fields.setdefault('message_id', self.generate_id())
        fields.setdefault('in_reply_to', None)
        fields.setdefault('session_event', None)
        fields.setdefault('content', None)
        fields.setdefault('transport_metadata', {})
        fields.setdefault('helper_metadata', {})
        fields.setdefault('group', None)
        return fields

    def validate_fields(self):
        super(TransportUserMessage, self).validate_fields()
        # We might get older message versions without the `group` field.
        self.payload.setdefault('group', None)
        self.assert_field_present(
            'message_id',
            'to_addr',
            'from_addr',
            'in_reply_to',
            'session_event',
            'content',
            'transport_name',
            'transport_type',
            'transport_metadata',
            'helper_metadata',
            'group',
            )
        if self['session_event'] not in self.SESSION_EVENTS:
            raise InvalidMessageField("Invalid session_event %r"
                                      % (self['session_event'],))

    def user(self):
        return self['from_addr']

    def reply(self, content, continue_session=True, **kw):
        """Construct a reply message.

        The reply message will have its `to_addr` field set to the original
        message's `from_addr`. This means that even if the original message is
        directed to the group only (i.e. it has `to_addr` set to `None`), the
        reply will be directed to the sender of the original message.

        :meth:`reply` suitable for constructing both one-to-one messages (such
        as SMS) and directed messages within a group chat (such as
        name-prefixed content in an IRC channel message).

        If `session_event` is provided in the the keyword args,
        `continue_session` will be ignored.

        NOTE: Certain fields are required to come from the message being
              replied to and may not be overridden by this method:

              # If we're not using this addressing, we shouldn't be replying.
              'to_addr', 'from_addr', 'group', 'in_reply_to',
              # These three belong together and are supposed to be opaque.
              'transport_name', 'transport_type', 'transport_metadata'

        FIXME: `helper_metadata` should *not* be copied to the reply message.
               We only do it here because a bunch of legacy code relies on it.
        """
        session_event = None if continue_session else self.SESSION_CLOSE

        for field in [
                # If we're not using this addressing, we shouldn't be replying.
                'to_addr', 'from_addr', 'group', 'in_reply_to',
                # These three belong together and are supposed to be opaque.
                'transport_name', 'transport_type', 'transport_metadata']:
            if field in kw:
                # Other "bad keyword argument" conditions cause TypeErrors.
                raise TypeError("'%s' may not be overridden." % (field,))

        fields = {
            'helper_metadata': self['helper_metadata'],  # XXX: See above.
            'session_event': session_event,
            'to_addr': self['from_addr'],
            'from_addr': self['to_addr'],
            'group': self['group'],
            'in_reply_to': self['message_id'],
            'transport_name': self['transport_name'],
            'transport_type': self['transport_type'],
            'transport_metadata': self['transport_metadata'],
        }
        fields.update(kw)

        out_msg = TransportUserMessage(content=content, **fields)
        # The reply should go out the same endpoint it came in.
        out_msg.set_routing_endpoint(self.get_routing_endpoint())
        return out_msg

    def reply_group(self, *args, **kw):
        """Construct a group reply message.

        If the `group` field is set to `None`, :meth:`reply_group` is identical
        to :meth:`reply`.

        If the `group` field is not set to `None`, the reply message will have
        its `to_addr` field set to `None`. This means that even if the original
        message is directed to an individual within the group (i.e. its
        `to_addr` is not set to `None`), the reply will be directed to the
        group as a whole.

        :meth:`reply_group` suitable for both one-to-one messages (such as SMS)
        and undirected messages within a group chat (such as IRC channel
        messages).
        """
        out_msg = self.reply(*args, **kw)
        if self['group'] is not None:
            out_msg['to_addr'] = None
        return out_msg

    @classmethod
    def send(cls, to_addr, content, **kw):
        kw.setdefault('from_addr', None)
        kw.setdefault('transport_name', None)
        kw.setdefault('transport_type', None)
        out_msg = cls(
            to_addr=to_addr,
            in_reply_to=None,
            content=content,
            session_event=cls.SESSION_NONE,
            **kw)
        return out_msg


class TransportEvent(TransportMessage):
    """Message about a TransportUserMessage.
    """
    MESSAGE_TYPE = 'event'

    # list of valid delivery statuses
    DELIVERY_STATUSES = frozenset(('pending', 'failed', 'delivered'))

    # map of event_types -> extra fields
    EVENT_TYPES = {
        'ack': {'sent_message_id': lambda v: v is not None},
        'nack': {
            'nack_reason': lambda v: v is not None,
        },
        'delivery_report': {
            'delivery_status': lambda v: v in TransportEvent.DELIVERY_STATUSES,
            },
        }

    def process_fields(self, fields):
        fields = super(TransportEvent, self).process_fields(fields)
        fields.setdefault('event_id', self.generate_id())
        return fields

    def validate_fields(self):
        super(TransportEvent, self).validate_fields()
        self.assert_field_present(
            'user_message_id',
            'event_id',
            'event_type',
            )
        event_type = self.payload['event_type']
        if event_type not in self.EVENT_TYPES:
            raise InvalidMessageField("Unknown event_type %r" % (event_type,))
        for extra_field, check in self.EVENT_TYPES[event_type].items():
            self.assert_field_present(extra_field)
            if not check(self[extra_field]):
                raise InvalidMessageField(extra_field)
