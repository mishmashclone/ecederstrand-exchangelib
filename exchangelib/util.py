import datetime
import io
import itertools
import logging
import re
import socket
import time
import xml.sax.handler  # nosec
from base64 import b64decode, b64encode
from codecs import BOM_UTF8
from collections import OrderedDict
from decimal import Decimal
from functools import wraps
from threading import get_ident
from urllib.parse import urlparse

import isodate
import lxml.etree  # nosec
import requests.exceptions
from defusedxml.expatreader import DefusedExpatParser
from defusedxml.sax import _InputSource
from oauthlib.oauth2 import TokenExpiredError
from pygments import highlight
from pygments.formatters.terminal import TerminalFormatter
from pygments.lexers.html import XmlLexer

from .errors import TransportError, RateLimitError, RedirectError, RelativeRedirect, CASError, UnauthorizedError, \
    ErrorInvalidSchemaVersionForMailboxVersion

log = logging.getLogger(__name__)
xml_log = logging.getLogger('%s.xml' % __name__)


def require_account(f):
    @wraps(f)
    def wrapper(self, *args, **kwargs):
        if not self.account:
            raise ValueError('%s must have an account' % self.__class__.__name__)
        return f(self, *args, **kwargs)
    return wrapper


def require_id(f):
    @wraps(f)
    def wrapper(self, *args, **kwargs):
        if not self.account:
            raise ValueError('%s must have an account' % self.__class__.__name__)
        if not self.id:
            raise ValueError('%s must have an ID' % self.__class__.__name__)
        return f(self, *args, **kwargs)
    return wrapper


class ParseError(lxml.etree.ParseError):
    """Used to wrap lxml ParseError in our own class"""


class ElementNotFound(Exception):
    def __init__(self, msg, data):
        super().__init__(msg)
        self.data = data


# Regex of UTF-8 control characters that are illegal in XML 1.0 (and XML 1.1)
_ILLEGAL_XML_CHARS_RE = re.compile('[\x00-\x08\x0b\x0c\x0e-\x1F\uD800-\uDFFF\uFFFE\uFFFF]')

# XML namespaces
SOAPNS = 'http://schemas.xmlsoap.org/soap/envelope/'
MNS = 'http://schemas.microsoft.com/exchange/services/2006/messages'
TNS = 'http://schemas.microsoft.com/exchange/services/2006/types'
ENS = 'http://schemas.microsoft.com/exchange/services/2006/errors'
AUTODISCOVER_BASE_NS = 'http://schemas.microsoft.com/exchange/autodiscover/responseschema/2006'
AUTODISCOVER_REQUEST_NS = 'http://schemas.microsoft.com/exchange/autodiscover/outlook/requestschema/2006'
AUTODISCOVER_RESPONSE_NS = 'http://schemas.microsoft.com/exchange/autodiscover/outlook/responseschema/2006a'

ns_translation = OrderedDict([
    ('s', SOAPNS),
    ('m', MNS),
    ('t', TNS),
])
for item in ns_translation.items():
    lxml.etree.register_namespace(*item)


def is_iterable(value, generators_allowed=False):
    """Checks if value is a list-like object. Don't match generators and generator-like objects here by default, because
    callers don't necessarily guarantee that they only iterate the value once. Take care to not match string types and
    bytes.

    Args:
      value: any type of object
      generators_allowed: if True, generators will be treated as iterable (Default value = False)

    Returns:
      True or False

    """
    if generators_allowed:
        if not isinstance(value, (bytes, str)) and hasattr(value, '__iter__'):
            return True
    else:
        if isinstance(value, (tuple, list, set)):
            return True
    return False


def chunkify(iterable, chunksize):
    """Splits an iterable into chunks of size ``chunksize``. The last chunk may be smaller than ``chunksize``.

    Args:
      iterable:
      chunksize:

    """
    from .queryset import QuerySet
    if hasattr(iterable, '__getitem__') and not isinstance(iterable, QuerySet):
        # tuple, list. QuerySet has __getitem__ but that evaluates the entire query greedily. We don't want that here.
        for i in range(0, len(iterable), chunksize):
            yield iterable[i:i + chunksize]
    else:
        # generator, set, map, QuerySet
        chunk = []
        for i in iterable:
            chunk.append(i)
            if len(chunk) == chunksize:
                yield chunk
                chunk = []
        if chunk:
            yield chunk


def peek(iterable):
    """Checks if an iterable is empty and returns status and the rewinded iterable

    Args:
      iterable:

    """
    if hasattr(iterable, '__len__'):
        # tuple, list, set
        return not iterable, iterable
    # generator
    try:
        first = next(iterable)
    except StopIteration:
        return True, iterable
    # We can't rewind a generator. Instead, chain the first element and the rest of the generator
    return False, itertools.chain([first], iterable)


def xml_to_str(tree, encoding=None, xml_declaration=False):
    """Serialize an XML tree. Returns unicode if 'encoding' is None. Otherwise, we return encoded 'bytes'.

    Args:
      tree:
      encoding:  (Default value = None)
      xml_declaration:  (Default value = False)

    """
    if xml_declaration and not encoding:
        raise ValueError("'xml_declaration' is not supported when 'encoding' is None")
    if encoding:
        return lxml.etree.tostring(tree, encoding=encoding, xml_declaration=True)
    return lxml.etree.tostring(tree, encoding=str, xml_declaration=False)


def get_xml_attr(tree, name):
    elem = tree.find(name)
    if elem is None:  # Must compare with None, see XML docs
        return None
    return elem.text or None


def get_xml_attrs(tree, name):
    return [elem.text for elem in tree.findall(name) if elem.text is not None]


def value_to_xml_text(value):
    from .ewsdatetime import EWSTimeZone, EWSDateTime, EWSDate
    from .indexed_properties import PhoneNumber, EmailAddress
    from .properties import Mailbox, AssociatedCalendarItemId, Attendee, ConversationId
    # We can't just create a map and look up with type(value) because we want to support subtypes
    if isinstance(value, str):
        return safe_xml_value(value)
    if isinstance(value, bool):
        return '1' if value else '0'
    if isinstance(value, bytes):
        return b64encode(value).decode('ascii')
    if isinstance(value, (int, Decimal)):
        return str(value)
    if isinstance(value, datetime.time):
        return value.isoformat()
    if isinstance(value, EWSTimeZone):
        return value.ms_id
    if isinstance(value, EWSDateTime):
        return value.ewsformat()
    if isinstance(value, EWSDate):
        return value.ewsformat()
    if isinstance(value, PhoneNumber):
        return value.phone_number
    if isinstance(value, EmailAddress):
        return value.email
    if isinstance(value, Mailbox):
        return value.email_address
    if isinstance(value, Attendee):
        return value.mailbox.email_address
    if isinstance(value, ConversationId):
        return value.id
    if isinstance(value, AssociatedCalendarItemId):
        return value.id
    raise TypeError('Unsupported type: %s (%s)' % (type(value), value))


def xml_text_to_value(value, value_type):
    from .ewsdatetime import EWSDate, EWSDateTime
    if value_type == str:
        return value
    if value_type == bool:
        try:
            return {
                'true': True,
                'on': True,
                'false': False,
                'off': False,
            }[value.lower()]
        except KeyError:
            return None
    return {
        bytes: safe_b64decode,
        int: int,
        Decimal: Decimal,
        datetime.timedelta: isodate.parse_duration,
        EWSDate: EWSDate.from_string,
        EWSDateTime: EWSDateTime.from_string,
    }[value_type](value)


def set_xml_value(elem, value, version):
    from .ewsdatetime import EWSDateTime, EWSDate
    from .fields import FieldPath, FieldOrder
    from .properties import EWSElement
    from .version import Version
    if isinstance(value, (str, bool, bytes, int, Decimal, datetime.time, EWSDate, EWSDateTime)):
        elem.text = value_to_xml_text(value)
    elif isinstance(value, _element_class):
        elem.append(value)
    elif is_iterable(value, generators_allowed=True):
        for v in value:
            if isinstance(v, (FieldPath, FieldOrder)):
                elem.append(v.to_xml())
            elif isinstance(v, EWSElement):
                if not isinstance(version, Version):
                    raise ValueError("'version' %r must be a Version instance" % version)
                elem.append(v.to_xml(version=version))
            elif isinstance(v, _element_class):
                elem.append(v)
            elif isinstance(v, str):
                add_xml_child(elem, 't:String', v)
            else:
                raise ValueError('Unsupported type %s for list element %s on elem %s' % (type(v), v, elem))
    elif isinstance(value, (FieldPath, FieldOrder)):
        elem.append(value.to_xml())
    elif isinstance(value, EWSElement):
        if not isinstance(version, Version):
            raise ValueError("'version' %r must be a Version instance" % version)
        elem.append(value.to_xml(version=version))
    else:
        raise ValueError('Unsupported type %s for value %s on elem %s' % (type(value), value, elem))
    return elem


def safe_xml_value(value, replacement='?'):
    return _ILLEGAL_XML_CHARS_RE.sub(replacement, value)


def create_element(name, attrs=None, nsmap=None):
    # Python versions prior to 3.6 do not preserve dict or kwarg ordering, so we cannot pull in attrs as **kwargs if we
    # also want stable XML attribute output. Instead, let callers supply us with an OrderedDict instance.
    if ':' in name:
        ns, name = name.split(':')
        name = '{%s}%s' % (ns_translation[ns], name)
    elem = _forgiving_parser.makeelement(name, nsmap=nsmap)
    if attrs:
        # Try hard to keep attribute order, to ensure deterministic output. This simplifies testing.
        for k, v in attrs.items():
            elem.set(k, v)
    return elem


def add_xml_child(tree, name, value):
    # We're calling add_xml_child many places where we don't have the version handy. Don't pass EWSElement or list of
    # EWSElement to this function!
    tree.append(set_xml_value(elem=create_element(name), value=value, version=None))


class StreamingContentHandler(xml.sax.handler.ContentHandler):
    """A SAX content handler that returns a character data for a single element back to the parser. The parser must have
    a 'buffer' attribute we can append data to.
    """

    def __init__(self, parser, ns, element_name):
        xml.sax.handler.ContentHandler.__init__(self)
        self._parser = parser
        self._ns = ns
        self._element_name = element_name
        self._parsing = False

    def startElementNS(self, name, qname, attrs):
        if name == (self._ns, self._element_name):
            # we can expect element data next
            self._parsing = True
            self._parser.element_found = True

    def endElementNS(self, name, qname):
        if name == (self._ns, self._element_name):
            # all element data received
            self._parsing = False

    def characters(self, content):
        if not self._parsing:
            return
        self._parser.buffer.append(content)


def prepare_input_source(source):
    # Extracted from xml.sax.expatreader.saxutils.prepare_input_source
    f = source
    source = _InputSource()
    source.setByteStream(f)
    return source


def safe_b64decode(data):
    # Incoming base64-encoded data is not always padded to a multiple of 4. Python's parser is more strict and requires
    # padding. Add padding if it's needed.
    overflow = len(data) % 4
    if overflow:
        if isinstance(data, str):
            padding = '=' * (4 - overflow)
        else:
            padding = b'=' * (4 - overflow)
        data += padding
    return b64decode(data)


class StreamingBase64Parser(DefusedExpatParser):
    """A SAX parser that returns a generator of base64-decoded character content"""

    def __init__(self, *args, **kwargs):
        DefusedExpatParser.__init__(self, *args, **kwargs)
        self._namespaces = True
        self.buffer = None
        self.element_found = None

    def parse(self, r):
        raw_source = r.raw
        # Like upstream but yields the return value of self.feed()
        raw_source = prepare_input_source(raw_source)
        self.prepareParser(raw_source)
        file = raw_source.getByteStream()
        self.buffer = []
        self.element_found = False
        buffer = file.read(self._bufsize)
        collected_data = []
        while buffer:
            if not self.element_found:
                collected_data += buffer
            yield from self.feed(buffer)
            buffer = file.read(self._bufsize)
        # Any remaining data in self.buffer should be padding chars now
        self.buffer = None
        r.close()  # Release memory
        self.close()
        if not self.element_found:
            data = bytes(collected_data)
            raise ElementNotFound('The element to be streamed from was not found', data=bytes(data))

    def feed(self, data, isFinal=0):
        # Like upstream, but yields the current content of the character buffer
        DefusedExpatParser.feed(self, data=data, isFinal=isFinal)
        return self._decode_buffer()

    def _decode_buffer(self):
        remainder = ''
        for data in self.buffer:
            available = len(remainder) + len(data)
            overflow = available % 4  # Make sure we always decode a multiple of 4
            if remainder:
                data = (remainder + data)
                remainder = ''
            if overflow:
                remainder, data = data[-overflow:], data[:-overflow]
            if data:
                yield b64decode(data)
        self.buffer = [remainder] if remainder else []


_forgiving_parser = lxml.etree.XMLParser(
    resolve_entities=False,  # This setting is recommended by lxml for safety
    recover=True,  # This setting is non-default
    huge_tree=True,  # This setting enables parsing huge attachments, mime_content and other large data
)
_element_class = _forgiving_parser.makeelement('x').__class__


class BytesGeneratorIO(io.RawIOBase):
    """A BytesIO that can produce bytes from a streaming HTTP request. Expects r.iter_content() as input
    lxml tries to be smart by calling `getvalue` when present, assuming that the entire string is in memory.
    Omitting `getvalue` forces lxml to stream the request through `read` avoiding the memory duplication.
    """

    def __init__(self, bytes_generator):
        self._bytes_generator = bytes_generator
        self._next = bytearray()
        self._tell = 0
        super().__init__()

    def readable(self):
        return not self.closed

    def tell(self):
        return self._tell

    def read(self, size=-1):
        # requests `iter_content()` auto-adjusts the number of bytes based on bandwidth
        # can't assume how many bytes next returns so stash any extra in `self._next`
        if self.closed:
            raise ValueError("read from a closed file")
        if self._next is None:
            return b''
        if size is None:
            size = -1

        res = self._next
        while size < 0 or len(res) < size:
            try:
                res.extend(next(self._bytes_generator))
            except StopIteration:
                self._next = None
                break

        if size > 0 and self._next is not None:
            self._next = res[size:]
            res = res[:size]

        self._tell += len(res)
        return bytes(res)

    def close(self):
        if not self.closed:
            self._bytes_generator.close()
        super().close()


class DocumentYielder:
    """Looks for XML documents in a streaming HTTP response and yields them as they become available from the stream"""

    def __init__(self, content_iterator, document_tag='Envelope'):
        self._iterator = content_iterator
        self._start_token = b'<%s' % document_tag.encode('utf-8')
        self._end_token = b'/%s>' % document_tag.encode('utf-8')

    def get_tag(self, stop_byte):
        tag_buffer = [b'<']
        while True:
            try:
                c = next(self._iterator)
            except StopIteration:
                break
            tag_buffer.append(c)
            if c == stop_byte:
                break
        return b''.join(tag_buffer)

    def __iter__(self):
        """Consumes the content iterator, looking for start and end tags. Returns each document when we have fully
        collected it.
        """
        doc_started = False
        buffer = []
        try:
            while True:
                c = next(self._iterator)
                if not doc_started and c == b'<':
                    tag = self.get_tag(stop_byte=b' ')
                    if tag.startswith(self._start_token):
                        # Start of document. Collect bytes from this point
                        buffer.append(tag)
                        doc_started = True
                elif doc_started and c == b'<':
                    tag = self.get_tag(stop_byte=b'>')
                    buffer.append(tag)
                    if tag.endswith(self._end_token):
                        # End of document. Yield a valid document and reset the buffer
                        yield b"<?xml version='1.0' encoding='utf-8'?>\n%s" % b''.join(buffer)
                        doc_started = False
                        buffer = []
                elif doc_started:
                    buffer.append(c)
        except StopIteration:
            return


def to_xml(bytes_content):
    # Converts bytes or a generator of bytes to an XML tree
    # Exchange servers may spit out the weirdest XML. lxml is pretty good at recovering from errors
    if isinstance(bytes_content, bytes):
        stream = io.BytesIO(bytes_content)
    else:
        stream = BytesGeneratorIO(bytes_content)
    try:
        res = lxml.etree.parse(stream, parser=_forgiving_parser)  # nosec
    except AssertionError as e:
        raise ParseError(e.args[0], '<not from file>', -1, 0)
    except lxml.etree.ParseError as e:
        if hasattr(e, 'position'):
            e.lineno, e.offset = e.position
        if not e.lineno:
            raise ParseError(str(e), '<not from file>', e.lineno, e.offset)
        try:
            stream.seek(0)
            offending_line = stream.read().splitlines()[e.lineno - 1]
        except (IndexError, io.UnsupportedOperation):
            raise ParseError(str(e), '<not from file>', e.lineno, e.offset)
        else:
            offending_excerpt = offending_line[max(0, e.offset - 20):e.offset + 20]
            msg = '%s\nOffending text: [...]%s[...]' % (str(e), offending_excerpt)
            raise ParseError(msg, '<not from file>', e.lineno, e.offset)
    except TypeError:
        try:
            stream.seek(0)
        except (IndexError, io.UnsupportedOperation):
            pass
        raise ParseError('This is not XML: %r' % stream.read(), '<not from file>', -1, 0)

    if res.getroot() is None:
        try:
            stream.seek(0)
            msg = 'No root element found: %r' % stream.read()
        except (IndexError, io.UnsupportedOperation):
            msg = 'No root element found'
        raise ParseError(msg, '<not from file>', -1, 0)
    return res


def is_xml(text, expected_prefix=b'<?xml'):
    """Lightweight test if response is an XML doc. It's better to be fast than correct here.

    Args:
      text: The string to check
      expected_prefix: What to search for in the start if the string

    """
    # BOM_UTF8 is an UTF-8 byte order mark which may precede the XML from an Exchange server
    bom_len = len(BOM_UTF8)
    prefix_len = len(expected_prefix)
    if text[:bom_len] == BOM_UTF8:
        prefix = text[bom_len:bom_len + prefix_len]
    else:
        prefix = text[:prefix_len]
    return prefix == expected_prefix


class PrettyXmlHandler(logging.StreamHandler):
    """A steaming log handler that prettifies log statements containing XML when output is a terminal"""

    @staticmethod
    def parse_bytes(xml_bytes):
        return lxml.etree.parse(io.BytesIO(xml_bytes), parser=_forgiving_parser)  # nosec

    @classmethod
    def prettify_xml(cls, xml_bytes):
        # Re-formats an XML document to a consistent style
        return lxml.etree.tostring(
            cls.parse_bytes(xml_bytes),
            xml_declaration=True,
            encoding='utf-8',
            pretty_print=True
        ).replace(b'\t', b'    ').replace(b' xmlns:', b'\n    xmlns:')

    @staticmethod
    def highlight_xml(xml_str):
        # Highlights a string containing XML, using terminal color codes
        return highlight(xml_str, XmlLexer(), TerminalFormatter()).rstrip()

    def emit(self, record):
        """Pretty-print and syntax highlight a log statement if all these conditions are met:
           * This is a DEBUG message
           * We're outputting to a terminal
           * The log message args is a dict containing keys starting with 'xml_' and values as bytes

        Args:
          record:

        """
        if record.levelno == logging.DEBUG and self.is_tty() and isinstance(record.args, dict):
            for key, value in record.args.items():
                if not key.startswith('xml_'):
                    continue
                if not isinstance(value, bytes):
                    continue
                if not is_xml(value):
                    continue
                try:
                    record.args[key] = self.highlight_xml(self.prettify_xml(value))
                except Exception as e:
                    # Something bad happened, but we don't want to crash the program just because logging failed
                    print('XML highlighting failed: %s' % e)
        return super().emit(record)

    def is_tty(self):
        # Check if we're outputting to a terminal
        try:
            return self.stream.isatty()
        except AttributeError:
            return False


class AnonymizingXmlHandler(PrettyXmlHandler):
    """A steaming log handler that prettifies and anonymizes log statements containing XML when output is a terminal"""

    def __init__(self, forbidden_strings, *args, **kwargs):
        self.forbidden_strings = forbidden_strings
        super().__init__(*args, **kwargs)

    def parse_bytes(self, xml_bytes):
        root = lxml.etree.parse(io.BytesIO(xml_bytes), parser=_forgiving_parser)  # nosec
        for elem in root.iter():
            for attr in set(elem.keys()) & {'RootItemId', 'ItemId', 'Id', 'RootItemChangeKey', 'ChangeKey'}:
                elem.set(attr, 'DEADBEEF=')
            for s in self.forbidden_strings:
                elem.text.replace(s, '[REMOVED]')
        return root


class DummyRequest:
    def __init__(self, headers):
        self.headers = headers


class DummyResponse:
    def __init__(self, url, headers, request_headers, content=b'', status_code=503, history=None):
        self.status_code = status_code
        self.url = url
        self.headers = headers
        self.content = content
        self.text = content.decode('utf-8', errors='ignore')
        self.request = DummyRequest(headers=request_headers)
        self.history = history

    def iter_content(self):
        return self.content

    def close(self):
        pass


def get_domain(email):
    try:
        return email.split('@')[1].lower()
    except (IndexError, AttributeError):
        raise ValueError("'%s' is not a valid email" % email)


def split_url(url):
    parsed_url = urlparse(url)
    # Use netloc instead of hostname since hostname is None if URL is relative
    return parsed_url.scheme == 'https', parsed_url.netloc.lower(), parsed_url.path


def get_redirect_url(response, allow_relative=True, require_relative=False):
    # allow_relative=False throws RelativeRedirect error if scheme and hostname are equal to the request
    # require_relative=True throws RelativeRedirect error if scheme and hostname are not equal to the request
    redirect_url = response.headers.get('location', None)
    if not redirect_url:
        raise TransportError('HTTP redirect but no location header')
    # At least some servers are kind enough to supply a new location. It may be relative
    redirect_has_ssl, redirect_server, redirect_path = split_url(redirect_url)
    # The response may have been redirected already. Get the original URL
    request_url = response.history[0] if response.history else response.url
    request_has_ssl, request_server, _ = split_url(request_url)
    response_has_ssl, response_server, response_path = split_url(response.url)

    if not redirect_server:
        # Redirect URL is relative. Inherit server and scheme from response URL
        redirect_server = response_server
        redirect_has_ssl = response_has_ssl
    if not redirect_path.startswith('/'):
        # The path is not top-level. Add response path
        redirect_path = (response_path or '/') + redirect_path
    redirect_url = '%s://%s%s' % ('https' if redirect_has_ssl else 'http', redirect_server, redirect_path)
    if redirect_url == request_url:
        # And some are mean enough to redirect to the same location
        raise TransportError('Redirect to same location: %s' % redirect_url)
    if not allow_relative and (request_has_ssl == response_has_ssl and request_server == redirect_server):
        raise RelativeRedirect(redirect_url)
    if require_relative and (request_has_ssl != response_has_ssl or request_server != redirect_server):
        raise RelativeRedirect(redirect_url)
    return redirect_url


RETRY_WAIT = 10  # Seconds to wait before retry on connection errors
MAX_REDIRECTS = 10  # Maximum number of URL redirects before we give up

# A collection of error classes we want to handle as general connection errors
CONNECTION_ERRORS = (requests.exceptions.ChunkedEncodingError, requests.exceptions.ConnectionError,
                     requests.exceptions.Timeout, socket.timeout, ConnectionResetError)

# A collection of error classes we want to handle as TLS verification errors
TLS_ERRORS = (requests.exceptions.SSLError,)
try:
    # If pyOpenSSL is installed, requests will use it and throw this class on TLS errors
    import OpenSSL.SSL
    TLS_ERRORS += (OpenSSL.SSL.Error,)
except ImportError:
    pass


def post_ratelimited(protocol, session, url, headers, data, allow_redirects=False, stream=False, timeout=None):
    """There are two error-handling policies implemented here: a fail-fast policy intended for stand-alone scripts which
    fails on all responses except HTTP 200. The other policy is intended for long-running tasks that need to respect
    rate-limiting errors from the server and paper over outages of up to 1 hour.

    Wrap POST requests in a try-catch loop with a lot of error handling logic and some basic rate-limiting. If a request
    fails, and some conditions are met, the loop waits in increasing intervals, up to 1 hour, before trying again. The
    reason for this is that servers often malfunction for short periods of time, either because of ongoing data
    migrations or other maintenance tasks, misconfigurations or heavy load, or because the connecting user has hit a
    throttling policy limit.

    If the loop exited early, consumers of this package that don't implement their own rate-limiting code could quickly
    swamp such a server with new requests. That would only make things worse. Instead, it's better if the request loop
    waits patiently until the server is functioning again.

    If the connecting user has hit a throttling policy, then the server will start to malfunction in many interesting
    ways, but never actually tell the user what is happening. There is no way to distinguish this situation from other
    malfunctions. The only cure is to stop making requests.

    The contract on sessions here is to return the session that ends up being used, or retiring the session if we
    intend to raise an exception. We give up on max_wait timeout, not number of retries.

    An additional resource on handling throttling policies and client back off strategies:
        https://docs.microsoft.com/en-us/exchange/client-developer/exchange-web-services/ews-throttling-in-exchange

    Args:
      protocol:
      session:
      url:
      headers:
      data:
      allow_redirects:  (Default value = False)
      stream:  (Default value = False)
      timeout:

    """
    if not timeout:
        timeout = protocol.TIMEOUT
    thread_id = get_ident()
    wait = RETRY_WAIT  # Initial retry wait. We double the value on each retry
    retry = 0
    redirects = 0
    log_msg = '''\
Retry: %(retry)s
Waited: %(wait)s
Timeout: %(timeout)s
Session: %(session_id)s
Thread: %(thread_id)s
Auth type: %(auth)s
URL: %(url)s
HTTP adapter: %(adapter)s
Allow redirects: %(allow_redirects)s
Streaming: %(stream)s
Response time: %(response_time)s
Status code: %(status_code)s
Request headers: %(request_headers)s
Response headers: %(response_headers)s'''
    xml_log_msg = '''\
Request XML: %(xml_request)s
Response XML: %(xml_response)s'''
    log_vals = dict(
        retry=retry,
        wait=wait,
        timeout=timeout,
        session_id=session.session_id,
        thread_id=thread_id,
        auth=session.auth,
        url=url,
        adapter=session.get_adapter(url),
        allow_redirects=allow_redirects,
        stream=stream,
        response_time=None,
        status_code=None,
        request_headers=headers,
        response_headers=None,
    )
    xml_log_vals = dict(
        xml_request=None,
        xml_response=None,
    )
    t_start = time.monotonic()
    try:
        while True:
            backed_off = _back_off_if_needed(protocol.retry_policy.back_off_until)
            if backed_off:
                # We may have slept for a long time. Renew the session.
                session = protocol.renew_session(session)
            log.debug('Session %s thread %s: retry %s timeout %s POST\'ing to %s after %ss wait', session.session_id,
                      thread_id, retry, timeout, url, wait)
            d_start = time.monotonic()
            # Always create a dummy response for logging purposes, in case we fail in the following
            r = DummyResponse(url=url, headers={}, request_headers=headers)
            try:
                r = session.post(url=url, headers=headers, data=data, allow_redirects=False, timeout=timeout,
                                 stream=stream)
            except TLS_ERRORS as e:
                # Don't retry on TLS errors. They will most likely be persistent.
                raise TransportError(str(e))
            except CONNECTION_ERRORS as e:
                log.debug('Session %s thread %s: connection error POST\'ing to %s', session.session_id, thread_id, url)
                r = DummyResponse(url=url, headers={'TimeoutException': e}, request_headers=headers)
            except TokenExpiredError as e:
                log.debug('Session %s thread %s: OAuth token expired; refreshing', session.session_id, thread_id)
                r = DummyResponse(url=url, headers={'TokenExpiredError': e}, request_headers=headers, status_code=401)
            except KeyError as e:
                if e.args[0] != 'www-authenticate':
                    raise
                log.debug('Session %s thread %s: auth headers missing from %s', session.session_id, thread_id, url)
                r = DummyResponse(url=url, headers={'KeyError': e}, request_headers=headers)
            finally:
                log_vals.update(
                    retry=retry,
                    wait=wait,
                    session_id=session.session_id,
                    url=str(r.url),
                    response_time=time.monotonic() - d_start,
                    status_code=r.status_code,
                    request_headers=r.request.headers,
                    response_headers=r.headers,
                )
                xml_log_vals.update(
                    xml_request=data,
                    xml_response='[STREAMING]' if stream else r.content,
                )
            log.debug(log_msg, log_vals)
            xml_log.debug(xml_log_msg, xml_log_vals)
            if _need_new_credentials(response=r):
                r.close()  # Release memory
                session = protocol.refresh_credentials(session)
                continue
            total_wait = time.monotonic() - t_start
            if _may_retry_on_error(response=r, retry_policy=protocol.retry_policy, wait=total_wait):
                r.close()  # Release memory
                log.info("Session %s thread %s: Connection error on URL %s (code %s). Cool down %s secs",
                         session.session_id, thread_id, r.url, r.status_code, wait)
                protocol.retry_policy.back_off(wait)
                retry += 1
                wait *= 2  # Increase delay for every retry
                continue
            if r.status_code in (301, 302):
                r.close()  # Release memory
                url, redirects = _redirect_or_fail(r, redirects, allow_redirects)
                continue
            break
    except (RateLimitError, RedirectError) as e:
        log.warning(e.value)
        protocol.retire_session(session)
        raise
    except Exception as e:
        # Let higher layers handle this. Add full context for better debugging.
        log.error('%s: %s\n%s\n%s', e.__class__.__name__, str(e), log_msg % log_vals, xml_log_msg % xml_log_vals)
        protocol.retire_session(session)
        raise
    if r.status_code == 500 and r.content and is_xml(r.content):
        # Some genius at Microsoft thinks it's OK to send a valid SOAP response as an HTTP 500
        log.debug('Got status code %s but trying to parse content anyway', r.status_code)
    elif r.status_code != 200:
        protocol.retire_session(session)
        try:
            _raise_response_errors(r, protocol)  # Always raises an exception
        finally:
            log.error('%s\n%s', log_msg % log_vals, xml_log_msg % xml_log_vals)
    log.debug('Session %s thread %s: Useful response from %s', session.session_id, thread_id, url)
    return r, session


def _back_off_if_needed(back_off_until):
    if back_off_until:
        sleep_secs = (back_off_until - datetime.datetime.now()).total_seconds()
        # The back off value may have expired within the last few milliseconds
        if sleep_secs > 0:
            log.warning('Server requested back off until %s. Sleeping %s seconds', back_off_until, sleep_secs)
            time.sleep(sleep_secs)
            return True
    return False


def _may_retry_on_error(response, retry_policy, wait):
    if response.status_code not in (301, 302, 401, 500, 503):
        # Don't retry if we didn't get a status code that we can hope to recover from
        log.debug('No retry: wrong status code %s', response.status_code)
        return False
    if retry_policy.fail_fast:
        log.debug('No retry: no fail-fast policy')
        return False
    if wait > retry_policy.max_wait:
        # We lost patience. Session is cleaned up in outer loop
        raise RateLimitError(
            'Max timeout reached', url=response.url, status_code=response.status_code, total_wait=wait)
    # The genericerrorpage.htm/internalerror.asp is ridiculous behaviour for random outages. Redirect to
    # '/internalsite/internalerror.asp' or '/internalsite/initparams.aspx' is caused by e.g. TLS certificate
    # f*ckups on the Exchange server.
    #
    # "Server Error in '/EWS' Application" has been seen in highly concurrent settings.
    if (response.status_code == 401) \
            or (response.headers.get('connection') == 'close') \
            or (response.status_code == 302 and response.headers.get('location', '').lower() ==
                '/ews/genericerrorpage.htm?aspxerrorpath=/ews/exchange.asmx') \
            or (response.status_code == 503) \
            or (response.status_code == 500 and b"Server Error in '/EWS' Application" in response.content):
        log.debug('Retry allowed: conditions met')
        return True
    return False


def _need_new_credentials(response):
    return response.status_code == 401 \
        and response.headers.get('TokenExpiredError')


def _redirect_or_fail(response, redirects, allow_redirects):
    # Retry with no delay. If we let requests handle redirects automatically, it would issue a GET to that
    # URL. We still want to POST.
    try:
        redirect_url = get_redirect_url(response=response, allow_relative=False)
    except RelativeRedirect as e:
        log.debug("'allow_redirects' only supports relative redirects (%s -> %s)", response.url, e.value)
        raise RedirectError(url=e.value)
    if not allow_redirects:
        raise TransportError('Redirect not allowed but we were redirected (%s -> %s)' % (response.url, redirect_url))
    log.debug('HTTP redirected to %s', redirect_url)
    redirects += 1
    if redirects > MAX_REDIRECTS:
        raise TransportError('Max redirect count exceeded')
    return redirect_url, redirects


def _raise_response_errors(response, protocol):
    cas_error = response.headers.get('X-CasErrorCode')
    if cas_error:
        if cas_error.startswith('CAS error:'):
            # Remove unnecessary text
            cas_error = cas_error.split(':', 1)[1].strip()
        raise CASError(cas_error=cas_error, response=response)
    if response.status_code == 500 and (b'The specified server version is invalid' in response.content or
                                        b'ErrorInvalidSchemaVersionForMailboxVersion' in response.content):
        raise ErrorInvalidSchemaVersionForMailboxVersion('Invalid server version')
    if b'The referenced account is currently locked out' in response.content:
        raise TransportError('The service account is currently locked out')
    if response.status_code == 401 and protocol.retry_policy.fail_fast:
        # This is a login failure
        raise UnauthorizedError('Invalid credentials for %s' % response.url)
    if 'TimeoutException' in response.headers:
        raise response.headers['TimeoutException']
    # This could be anything. Let higher layers handle this
    raise TransportError(
        'Unknown failure in response. Code: %s headers: %s content: %s'
        % (response.status_code, response.headers, response.text)
    )
