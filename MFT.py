#!/bin/python

#    This file is part of INDXParse.
#
#   Copyright 2011 Will Ballenthin <william.ballenthin@mandiant.com>
#                    while at Mandiant <http://www.mandiant.com>
#
#   Licensed under the Apache License, Version 2.0 (the "License");
#   you may not use this file except in compliance with the License.
#   You may obtain a copy of the License at
#
#       http://www.apache.org/licenses/LICENSE-2.0
#
#   Unless required by applicable law or agreed to in writing, software
#   distributed under the License is distributed on an "AS IS" BASIS,
#   WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
#   See the License for the specific language governing permissions and
#   limitations under the License.
#
#   Version v.1.1.8
import array
import os
import sys
import struct
from datetime import datetime

from BinaryParser import Block
from BinaryParser import Nestable
from BinaryParser import memoize
from BinaryParser import align
from BinaryParser import warning
from BinaryParser import debug
from BinaryParser import ParseException
from BinaryParser import OverrunBufferException
from BinaryParser import read_byte
from BinaryParser import read_word
from BinaryParser import read_dword


class INDXException(Exception):
    """
    Base Exception class for INDX parsing.
    """
    def __init__(self, value):
        """
        Constructor.
        Arguments:
        - `value`: A string description.
        """
        super(INDXException, self).__init__()
        self._value = value

    def __str__(self):
        return "INDX Exception: %s" % (self._value)


class FixupBlock(Block):
    def __init__(self, buf, offset, parent):
        super(FixupBlock, self).__init__(buf, offset)

    def fixup(self, num_fixups, fixup_value_offset):
        fixup_value = self.unpack_word(fixup_value_offset)

        for i in range(0, num_fixups - 1):
            fixup_offset = 512 * (i + 1) - 2
            check_value = self.unpack_word(fixup_offset)

            if check_value != fixup_value:
                warning("Bad fixup at %s" % \
                            (hex(self.offset() + fixup_offset)))
                continue

            new_value = self.unpack_word(fixup_value_offset + 2 + 2 * i)
            self.pack_word(fixup_offset, new_value)

            check_value = self.unpack_word(fixup_offset)
            debug("Fixup verified at %s and patched from %s to %s." % \
                  (hex(self.offset() + fixup_offset),
                   hex(fixup_value), hex(check_value)))


class INDEX_ENTRY_FLAGS:
    """
    sizeof() == WORD
    """
    INDEX_ENTRY_NODE = 0x1
    INDEX_ENTRY_END = 0x2
    INDEX_ENTRY_SPACE_FILLER = 0xFFFF


class INDEX_ENTRY_HEADER(Block, Nestable):
    def __init__(self, buf, offset, parent):
        super(INDEX_ENTRY_HEADER, self).__init__(buf, offset)
        self.declare_field("word", "length", 0x8)
        self.declare_field("word", "key_length")
        self.declare_field("word", "index_entry_flags")  # see INDEX_ENTRY_FLAGS
        self.declare_field("word", "reserved")

    @staticmethod
    def structure_size(buf, offset, parent):
        return 0x10

    def __len__(self):
        return 0x10


class MFT_INDEX_ENTRY_HEADER(INDEX_ENTRY_HEADER):
    """
    Index used by the MFT for INDX attributes.
    """
    def __init__(self, buf, offset, parent):
        super(MFT_INDEX_ENTRY_HEADER, self).__init__(buf, offset, parent)
        self.declare_field("qword", "mft_reference", 0x0)


class SECURE_INDEX_ENTRY_HEADER(INDEX_ENTRY_HEADER):
    """
    Index used by the $SECURE file indices SII and SDH
    """
    def __init__(self, buf, offset, parent):
        super(SECURE_INDEX_ENTRY_HEADER, self).__init__(buf, offset, parent)
        self.declare_field("word", "data_offset", 0x0)
        self.declare_field("word", "data_length")
        self.declare_field("dword", "reserved")


class INDEX_ENTRY(Block,  Nestable):
    """
    NOTE: example structure. See the more specific classes below.
      Probably do not instantiate.
    """
    def __init__(self, buf, offset, parent):
        super(INDEX_ENTRY, self).__init__(buf, offset)
        self.declare_field(INDEX_ENTRY_HEADER, "header", 0x0)
        self.add_explicit_field(0x10, "string", "data")

    def data(self):
        start = self.offset() + 0x10
        end = start + self.header().key_length()
        return self._buf[start:end]

    @staticmethod
    def structure_size(buf, offset, parent):
        return read_word(buf, offset + 0x8)

    def __len__(self):
        return self.header().length()

    def is_valid(self):
        return True


class MFT_INDEX_ENTRY(Block, Nestable):
    def __init__(self, buf, offset, parent):
        super(MFT_INDEX_ENTRY, self).__init__(buf, offset)
        self.declare_field(MFT_INDEX_ENTRY_HEADER, "header", 0x0)
        self.declare_field(FilenameAttribute, "filename_information")

    @staticmethod
    def structure_size(buf, offset, parent):
        return read_word(buf, offset + 0x8)

    def __len__(self):
        return self.header().length()

    def is_valid(self):
        # this is a bit of a mess, but it should work
        recent_date = datetime(1990, 1, 1, 0, 0, 0)
        future_date = datetime(2025, 1, 1, 0, 0, 0)
        try:
            fn = self.filename_information()
        except:
            return False
        if not fn:
            return False
        try:
            return fn.modified_time() > recent_date and \
                   fn.accessed_time() > recent_date and \
                   fn.changed_time() > recent_date and \
                   fn.created_time() > recent_date and \
                   fn.modified_time() < future_date and \
                   fn.accessed_time() < future_date and \
                   fn.changed_time() < future_date and \
                   fn.created_time() < future_date
        except ValueError:
            return False


class SII_INDEX_ENTRY(Block, Nestable):
    def __init__(self, buf, offset, parent):
        super(SII_INDEX_ENTRY, self).__init__(buf, offset)
        self.declare_field(SECURE_INDEX_ENTRY_HEADER, "header", 0x0)
        self.declare_field("dword", "security_id")

    @staticmethod
    def structure_size(buf, offset, parent):
        return read_word(buf, offset + 0x8)

    def __len__(self):
        return self.header().length()

    def is_valid(self):
        # TODO(wb): test
        return 1 < self.header().length() < 0x30 and \
            1 < self.header().key_lenght() < 0x20


class SDH_INDEX_ENTRY(Block, Nestable):
    def __init__(self, buf, offset, parent):
        super(SDH_INDEX_ENTRY, self).__init__(buf, offset)
        self.declare_field(SECURE_INDEX_ENTRY_HEADER, "header", 0x0)
        self.declare_field("dword", "hash")
        self.declare_field("dword", "security_id")

    @staticmethod
    def structure_size(buf, offset, parent):
        return read_word(buf, offset + 0x8)

    def __len__(self):
        return self.header().length()

    def is_valid(self):
        # TODO(wb): test
        return 1 < self.header().length() < 0x30 and \
            1 < self.header().key_lenght() < 0x20


class INDEX_HEADER_FLAGS:
    SMALL_INDEX = 0x0  # MFT: INDX_ROOT only
    LARGE_INDEX = 0x1  # MFT: requires INDX_ALLOCATION
    LEAF_NODE = 0x1
    INDEX_NODE = 0x2
    NODE_MASK = 0x1


class INDEX_HEADER(Block, Nestable):
    def __init__(self, buf, offset, parent):
        super(INDEX_HEADER, self).__init__(buf, offset)
        self.declare_field("dword", "entries_offset", 0x0)
        self.declare_field("dword", "index_length")
        self.declare_field("dword", "allocated_size")
        self.declare_field("byte", "index_header_flags")  # see INDEX_HEADER_FLAGS
        # then 3 bytes padding/reserved

    @staticmethod
    def structure_size(buf, offset, parent):
        return 0x1C

    def __len__(self):
        return 0x1C


class INDEX(Block, Nestable):
    def __init__(self, buf, offset, parent, index_entry_class):
        self._INDEX_ENTRY = index_entry_class
        super(INDEX, self).__init__(buf, offset)
        self.declare_field(INDEX_HEADER, "header", 0x0)
        self.add_explicit_field(self.header().entries_offset(),
                                INDEX_ENTRY, "entries")
        start = self.header().entries_offset() + self.header().index_length()
        self.add_explicit_field(start, INDEX_ENTRY, "slack_entries")

    @staticmethod
    def structure_size(buf, offset, parent):
        return read_dword(buf, offset + 0x8)

    def __len__(self):
        return self.header().allocated_size()

    def entries(self):
        """
        A generator that returns each INDEX_ENTRY associated with this node.
        """
        offset = self.header().entries_offset()
        if offset == 0:
            debug("No entries in this allocation block.")
            return
        while offset <= self.header().index_length() - 0x52:
            debug("Entry has another entry after it.")
            e = self._INDEX_ENTRY(self._buf, self.offset() + offset, self)
            offset += e.length()
            yield e
        debug("No more entries.")

    def slack_entries(self):
        """
        A generator that yields INDEX_ENTRYs found in the slack space
        associated with this header.
        """
        offset = self.header().index_length()
        try:
            while offset <= self.header().allocated_size - 0x52:
                try:
                    debug("Trying to find slack entry at %s." % (hex(offset)))
                    e = self._INDEX_ENTRY(self._buf, offset, self)
                    if e.is_valid():
                        debug("Slack entry is valid.")
                        offset += e.length() or 1
                        yield e
                    else:
                        debug("Slack entry is invalid.")
                        raise ParseException("Not a deleted entry")
                except ParseException:
                    debug("Scanning one byte forward.")
                    offset += 1
        except struct.error:
            debug("Slack entry parsing overran buffer.")
            pass


def INDEX_ROOT(Block, Nestable):
    def __init__(self, buf, offset, parent):
        super(INDEX_ROOT, self).__init__(buf, offset)
        self.declare_field("dword", "type", 0x0)
        self.declare_field("dword", "collation_rule")
        self.declare_field("dword", "index_record_size_bytes")
        self.declare_field("byte",  "index_record_size_clusters")
        self.declare_field("byte", "unused1")
        self.declare_field("byte", "unused2")
        self.declare_field("byte", "unused3")
        self._index_offset = self.current_field_offset()
        self.add_explicit_field(self._index_offset, INDEX, "index")

    def index(self):
        return INDEX(self._buf, self.offset(self._index_offset),
                     self, MFT_INDEX_ENTRY)

    @staticmethod
    def structure_size(buf, offset, parent):
        return 0x10 + INDEX.structure_size(buf, offset + 0x10, parent)

    def __len__(self):
        return 0x10 + len(self.index())


class INDEX_ALLOCATION(FixupBlock):
    def __init__(self, buf, offset, parent):
        super(IndexRecordHeader, self).__init__(buf, offset, parent)
        self.declare_field("dword", "magic", 0x0)
        self.declare_field("word",  "usa_offset")
        self.declare_field("word",  "usa_count")
        self.declare_field("qword", "lsn")
        self.declare_field("qword", "vcn")
        self._index_offset = self.current_field_offset()
        self.add_explicit_field(self._index_offset, INDEX, "index")
        # TODO(wb): we do not want to modify data here.
        #   best to make a copy and use that.
        #   Until then, this is not a nestable structure.
        self.fixup(self.usa_count(), self.usa_offset())

    def index(self):
        return INDEX(self._buf, self.offset(self._index_offset),
                     self, MFT_INDEX_ENTRY)

    @staticmethod
    def structure_size(buf, offset, parent):
        return 0x30 + INDEX.structure_size(buf, offset + 0x10, parent)

    def __len__(self):
        return 0x30 + len(self.index())


class IndexRootHeader(Block):
    def __init__(self, buf, offset, parent):
        debug("INDEX ROOT HEADER at %s." % (hex(offset)))
        super(IndexRootHeader, self).__init__(buf, offset)
        self.declare_field("dword", "type", 0x0)
        self.declare_field("dword", "collation_rule")
        self.declare_field("dword", "index_record_size_bytes")
        self.declare_field("byte",  "index_record_size_clusters")
        self.declare_field("byte", "unused1")
        self.declare_field("byte", "unused2")
        self.declare_field("byte", "unused3")
        self._node_header_offset = self.current_field_offset()

    def node_header(self):
        return NTATTR_STANDARD_INDEX_HEADER(self._buf,
                               self.offset() + self._node_header_offset,
                               self)


class IndexRecordHeader(FixupBlock):
    def __init__(self, buf, offset, parent):
        debug("INDEX RECORD HEADER at %s." % (hex(offset)))
        super(IndexRecordHeader, self).__init__(buf, offset, parent)
        self.declare_field("dword", "magic", 0x0)
        self.declare_field("word",  "usa_offset")
        self.declare_field("word",  "usa_count")
        self.declare_field("qword", "lsn")
        self.declare_field("qword", "vcn")
        self._node_header_offset = self.current_field_offset()
        self.fixup(self.usa_count(), self.usa_offset())

    def node_header(self):
        return NTATTR_STANDARD_INDEX_HEADER(self._buf,
                               self.offset() + self._node_header_offset,
                               self)


class NTATTR_STANDARD_INDEX_HEADER(Block):
    def __init__(self, buf, offset, parent):
        debug("INDEX NODE HEADER at %s." % (hex(offset)))
        super(NTATTR_STANDARD_INDEX_HEADER, self).__init__(buf, offset)
        self.declare_field("dword", "entry_list_start", 0x0)
        self.declare_field("dword", "entry_list_end")
        self.declare_field("dword", "entry_list_allocation_end")
        self.declare_field("dword", "flags")
        self.declare_field("binary", "list_buffer", \
                           self.entry_list_start(),
                           self.entry_list_allocation_end() - self.entry_list_start())

    def entries(self):
        """
        A generator that returns each INDX entry associated with this node.
        """
        offset = self.entry_list_start()
        if offset == 0:
            debug("No entries in this allocation block.")
            return
        while offset <= self.entry_list_end() - 0x52:
            debug("Entry has another entry after it.")
            e = IndexEntry(self._buf, self.offset() + offset, self)
            offset += e.length()
            yield e
        debug("No more entries.")

    def slack_entries(self):
        """
        A generator that yields INDX entries found in the slack space
        associated with this header.
        """
        offset = self.entry_list_end()
        try:
            while offset <= self.entry_list_allocation_end() - 0x52:
                try:
                    debug("Trying to find slack entry at %s." % (hex(offset)))
                    e = SlackIndexEntry(self._buf, offset, self)
                    if e.is_valid():
                        debug("Slack entry is valid.")
                        offset += e.length() or 1
                        yield e
                    else:
                        debug("Slack entry is invalid.")
                        raise ParseException("Not a deleted entry")
                except ParseException:
                    debug("Scanning one byte forward.")
                    offset += 1
        except struct.error:
            debug("Slack entry parsing overran buffer.")
            pass


class IndexEntry(Block):
    def __init__(self, buf, offset, parent):
        debug("INDEX ENTRY at %s." % (hex(offset)))
        super(IndexEntry, self).__init__(buf, offset)
        self.declare_field("qword", "mft_reference", 0x0)
        self.declare_field("word", "length")
        self.declare_field("word", "filename_information_length")
        self.declare_field("dword", "flags")
        self.declare_field("binary", "filename_information_buffer", \
                           self.current_field_offset(),
                           self.filename_information_length())
        self.declare_field("qword", "child_vcn",
                           align(self.current_field_offset(), 0x8))

    def filename_information(self):
        return FilenameAttribute(self._buf,
                                 self.offset() + self._off_filename_information_buffer,
                                 self)


class StandardInformationFieldDoesNotExist(Exception):
    def __init__(self, msg):
        self._msg = msg

    def __str__(self):
        return "Standard Information attribute field does not exist: %s" % (self._msg)


class StandardInformation(Block):
    def __init__(self, buf, offset, parent):
        debug("STANDARD INFORMATION ATTRIBUTE at %s." % (hex(offset)))
        super(StandardInformation, self).__init__(buf, offset)
        self.declare_field("filetime", "created_time", 0x0)
        self.declare_field("filetime", "modified_time")
        self.declare_field("filetime", "changed_time")
        self.declare_field("filetime", "accessed_time")
        self.declare_field("dword", "attributes")
        self.declare_field("binary", "reserved",
                           self.current_field_offset(), 0xC)
        # self.declare_field("dword", "owner_id", 0x30)  # Win2k+
        # self.declare_field("dword", "security_id")  # Win2k+
        # self.declare_field("qword", "quota_charged")  # Win2k+
        # self.declare_field("qword", "usn")  # Win2k+

    def owner_id(self):
        """
        @raises StandardInformationFieldDoesNotExist
        """
        try:
            return self.unpack_dword(0x30)
        except OverrunBufferException:
            raise StandardInformationFieldDoesNotExist("Owner ID")

    def security_id(self):
        """
        @raises StandardInformationFieldDoesNotExist
        """
        try:
            return self.unpack_dword(0x34)
        except OverrunBufferException:
            raise StandardInformationFieldDoesNotExist("Security ID")

    def quota_charged(self):
        """
        @raises StandardInformationFieldDoesNotExist
        """
        try:
            return self.unpack_dword(0x38)
        except OverrunBufferException:
            raise StandardInformationFieldDoesNotExist("Quota Charged")

    def usn(self):
        """
        @raises StandardInformationFieldDoesNotExist
        """
        try:
            return self.unpack_dword(0x40)
        except OverrunBufferException:
            raise StandardInformationFieldDoesNotExist("USN")


class FilenameAttribute(Block, Nestable):
    def __init__(self, buf, offset, parent):
        debug("FILENAME ATTRIBUTE at %s." % (hex(offset)))
        super(FilenameAttribute, self).__init__(buf, offset)
        self.declare_field("qword", "mft_parent_reference", 0x0)
        self.declare_field("filetime", "created_time")
        self.declare_field("filetime", "modified_time")
        self.declare_field("filetime", "changed_time")
        self.declare_field("filetime", "accessed_time")
        self.declare_field("qword", "physical_size")
        self.declare_field("qword", "logical_size")
        self.declare_field("dword", "flags")
        self.declare_field("dword", "reparse_value")
        self.declare_field("byte", "filename_length")
        self.declare_field("byte", "filename_type")
        self.declare_field("wstring", "filename", 0x42, self.filename_length())

    @staticmethod
    def structure_size(buf, offset, parent):
        return 0x42 + (read_byte(buf, offset + 0x40) * 2)

    def __len__(self):
        return 0x42 + (self.filename_length() * 2)


class SlackIndexEntry(IndexEntry):
    def __init__(self, buf, offset, parent):
        """
        Constructor.
        Arguments:
        - `buf`: Byte string containing NTFS INDX file
        - `offset`: The offset into the buffer at which the block starts.
        - `parent`: The parent NTATTR_STANDARD_INDEX_HEADER block,
            which links to this block.
        """
        super(SlackIndexEntry, self).__init__(buf, offset, parent)

    def is_valid(self):
        # this is a bit of a mess, but it should work
        recent_date = datetime(1990, 1, 1, 0, 0, 0)
        future_date = datetime(2025, 1, 1, 0, 0, 0)
        try:
            fn = self.filename_information()
        except:
            return False
        if not fn:
            return False
        try:
            return fn.modified_time() > recent_date and \
                   fn.accessed_time() > recent_date and \
                   fn.changed_time() > recent_date and \
                   fn.created_time() > recent_date and \
                   fn.modified_time() < future_date and \
                   fn.accessed_time() < future_date and \
                   fn.changed_time() < future_date and \
                   fn.created_time() < future_date
        except ValueError:
            return False


class Runentry(Block):
    def __init__(self, buf, offset, parent):
        super(Runentry, self).__init__(buf, offset)
        debug("RUNENTRY @ %s." % (hex(offset)))
        self.declare_field("byte", "header")
        self._offset_length = self.header() >> 4
        self._length_length = self.header() & 0xF
        self.declare_field("binary",
                           "length_binary",
                           self.current_field_offset(), self._length_length)
        self.declare_field("binary",
                           "offset_binary",
                           self.current_field_offset(), self._offset_length)

    def is_valid(self):
        return self._offset_length > 0 and self._length_length > 0

    def lsb2num(self, binary):
        count = 0
        ret = 0
        for b in binary:
            ret += ord(b) << (8 * count)
            count += 1
        return ret

    def lsb2signednum(self, binary):
        count = 0
        ret = 0
        working = []

        is_negative = (ord(binary[-1]) & (1 << 7) != 0)
        if is_negative:
            working = [ord(b) ^ 0xFF for b in binary]
        else:
            working = [ord(b) for b in binary]
        for b in working:
            ret += b << (8 * count)
            count += 1
        if is_negative:
            ret += 1
            ret *= -1
        return ret

    def offset(self):
        return self.lsb2signednum(self.offset_binary())

    def length(self):
        return self.lsb2num(self.length_binary())

    def size(self):
        return self.current_field_offset()


class Runlist(Block):
    def __init__(self, buf, offset, parent):
        super(Runlist, self).__init__(buf, offset)
        debug("RUNLIST @ %s." % (hex(offset)))

    def _entries(self, length=None):
        ret = []
        offset = self.offset()
        entry = Runentry(self._buf, offset, self)
        while entry.header() != 0 and \
              (not length or offset < self.offset() + length) and \
              entry.is_valid():
            ret.append(entry)
            offset += entry.size()
            entry = Runentry(self._buf, offset, self)
        return ret

    def runs(self, length=None):
        """
        Yields tuples (volume offset, length).
        Recall that the entries are relative to one another
        """
        last_offset = 0
        for e in self._entries(length=length):
            current_offset = last_offset + e.offset()
            current_length = e.length()
            last_offset = current_offset
            yield (current_offset, current_length)


class ATTR_TYPE:
    STANDARD_INFORMATION = 0x10
    FILENAME_INFORMATION = 0x30
    DATA = 0x80
    INDEX_ROOT = 0x90
    INDEX_ALLOCATION = 0xA0


class Attribute(Block):
    TYPES = {
      16: "$STANDARD INFORMATION",
      32: "$ATTRIBUTE LIST",
      48: "$FILENAME INFORMATION",
      64: "$OBJECT ID/$VOLUME VERSION",
      80: "$SECURITY DESCRIPTOR",
      96: "$VOLUME NAME",
      112: "$VOLUME INFORMATION",
      128: "$DATA",
      144: "$INDEX ROOT",
      160: "$INDEX ALLOCATION",
      176: "$BITMAP",
      192: "$SYMBOLIC LINK",
      208: "$REPARSE POINT/$EA INFORMATION",
      224: "$EA",
      256: "$LOGGED UTILITY STREAM",
    }

    def __init__(self, buf, offset, parent):
        super(Attribute, self).__init__(buf, offset)
        debug("ATTRIBUTE @ %s." % (hex(offset)))
        self.declare_field("dword", "type")
        self.declare_field("dword", "size")
        self.declare_field("byte", "non_resident")
        self.declare_field("byte", "name_length")
        self.declare_field("word", "name_offset")
        self.declare_field("word", "flags")
        self.declare_field("word", "instance")
        if self.non_resident() > 0:
            self.declare_field("qword", "lowest_vcn", 0x10)
            self.declare_field("qword", "highest_vcn")
            self.declare_field("word", "runlist_offset")
            self.declare_field("byte", "compression_unit")
            self.declare_field("byte", "reserved1")
            self.declare_field("byte", "reserved2")
            self.declare_field("byte", "reserved3")
            self.declare_field("byte", "reserved4")
            self.declare_field("byte", "reserved5")
            self.declare_field("qword", "allocated_size")
            self.declare_field("qword", "data_size")
            self.declare_field("qword", "initialized_size")
            self.declare_field("qword", "compressed_size")
        else:
            self.declare_field("dword", "value_length", 0x10)
            self.declare_field("word", "value_offset")
            self.declare_field("byte", "value_flags")
            self.declare_field("byte", "reserved")
            self.declare_field("binary", "value",
                               self.value_offset(), self.value_length())

    def runlist(self):
        return Runlist(self._buf, self.offset() + self.runlist_offset(), self)

    def size(self):
        s = self.unpack_dword(self._off_size)
        return s + (8 - (s % 8))

    def name(self):
        return self.unpack_wstring(self.name_offset(), self.name_length())


class MFTRecord(FixupBlock):
    def __init__(self, buf, offset, parent, inode=None):
        super(MFTRecord, self).__init__(buf, offset, parent)
        debug("MFTRECORD @ %s." % (hex(offset)))
        self.inode = inode or 0
        self.declare_field("dword", "magic")
        self.declare_field("word",  "usa_offset")
        self.declare_field("word",  "usa_count")
        self.declare_field("qword", "lsn")
        self.declare_field("word",  "sequence_number")
        self.declare_field("word",  "link_count")
        self.declare_field("word",  "attrs_offset")
        self.declare_field("word",  "flags")
        self.declare_field("dword", "bytes_in_use")
        self.declare_field("dword", "bytes_allocated")
        self.declare_field("qword", "base_mft_record")
        self.declare_field("word",  "next_attr_instance")
        self.declare_field("word",  "reserved")
        self.declare_field("dword", "mft_record_number")

        self.fixup(self.usa_count(), self.usa_offset())

    def attributes(self):
        offset = self.attrs_offset()

        while self.unpack_dword(offset) != 0 and \
              self.unpack_dword(offset) != 0xFFFFFFFF and \
              offset + self.unpack_dword(offset + 4) <= self.offset() + self.bytes_in_use():
            a = Attribute(self._buf, offset, self)
            offset += a.size()
            yield a

    def attribute(self, attr_type):
        for a in self.attributes():
            if a.type() == attr_type:
                return a

    def is_directory(self):
        return self.flags() & 0x0002

    def is_active(self):
        # TODO what about link count == 0?
        return self.flags() & 0x0001

    # this a required resident attribute
    def filename_information(self):
        """
        MFT Records may have more than one FN info attribute,
        each with a different type of filename (8.3, POSIX, etc.)
        This one tends towards WIN32.
        """
        fn = False
        for a in self.attributes():
            # TODO optimize to self._buf here
            if a.type() == ATTR_TYPE.FILENAME_INFORMATION:
                try:
                    value = a.value()
                    check = FilenameAttribute(value, 0, self)
                    if check.filename_type() == 0x0001 or \
                       check.filename_type() == 0x0003:
                        return check
                    fn = check
                except Exception:
                    pass
        return fn

    # this a required resident attribute
    def standard_information(self):
        try:
            attr = self.attribute(ATTR_TYPE.STANDARD_INFORMATION)
            return StandardInformation(attr.value(), 0, self)
        except AttributeError as e:
            print(str(e))
            return False

    def data_attribute(self):
        """
        Returns None if the default $DATA attribute does not exist
        """
        for attr in self.attributes():
            if attr.type() == ATTR_TYPE.DATA and attr.name() == "":
                return attr


class InvalidAttributeException(INDXException):
    def __init__(self, value):
        super(InvalidAttributeException, self).__init__(value)

    def __str__(self):
        return "Invalid attribute Exception(%s)" % (self._value)


class InvalidMFTRecordNumber(Exception):
    def __init__(self, value):
        self.value = value


class NTFSFile():
    def __init__(self, options):
        if type(options) == dict:
            self.filename  = options["filename"]
            self.filetype  = options["filetype"] or "mft"
            self.offset    = options["offset"] or 0
            self.clustersize = options["clustersize"] or 4096
            self.mftoffset = False
            self.prefix    = options["prefix"] or None
            self.progress  = options["progress"]
        else:
            self.filename  = options.filename
            self.filetype  = options.filetype
            self.offset    = options.offset
            self.clustersize = options.clustersize
            self.mftoffset = False
            self.prefix    = options.prefix
            self.progress  = options.progress

    # TODO calculate cluster size

    def _calculate_mftoffset(self):
        with open(self.filename, "rb") as f:
            f.seek(self.offset)
            f.seek(0x30, 1)  # relative
            buf = f.read(8)
            relmftoffset = struct.unpack_from("<Q", buf, 0)[0]
            self.mftoffset = self.offset + relmftoffset * self.clustersize
            debug("MFT offset is %s" % (hex(self.mftoffset)))

    def record_generator(self):
        if self.filetype == "indx":
            return
        if self.filetype == "mft":
            size = os.path.getsize(self.filename)
            is_redirected = os.fstat(0) != os.fstat(1)
            should_progress = is_redirected and self.progress
            with open(self.filename, "rb") as f:
                record = True
                count = -1
                while record:
                    if count % 100 == 0 and should_progress:
                        n = (count * 1024 * 100) / float(size)
                        sys.stderr.write("\rCompleted: %0.4f%%" % (n))
                        sys.stderr.flush()
                    count += 1
                    buf = array.array("B", f.read(1024))
                    if not buf:
                        return
                    try:
                        record = MFTRecord(buf, 0, False, inode=count)
                    except OverrunBufferException:
                        debug("Failed to parse MFT record %s" % (str(count)))
                        continue
                    debug("Yielding record " + str(count))
                    yield record
            if should_progress:
                sys.stderr.write("\n")
        if self.filetype == "image":
            # TODO this overruns the MFT...
            # TODO this doesnt account for a fragmented MFT
            with open(self.filename, "rb") as f:
                if not self.mftoffset:
                    self._calculate_mftoffset()
                f.seek(self.mftoffset)
                record = True
                count = -1
                while record:
                    count += 1
                    buf = array.array("B", f.read(1024))
                    if not buf:
                        return
                    try:
                        record = MFTRecord(buf, 0, False, inode=count)
                    except OverrunBufferException:
                        debug("Failed to parse MFT record %s" % (str(count)))
                        continue
                    debug("Yielding record " + str(count))
                    yield record

    def mft_get_record_buf(self, number):
        if self.filetype == "indx":
            return array.array("B", "")
        if self.filetype == "mft":
            with open(self.filename, "rb") as f:
                f.seek(number * 1024)
                return array.array("B", f.read(1024))
        if self.filetype == "image":
            with open(self.filename, "rb") as f:
                f.seek(number * 1024)
                if not self.mftoffset:
                    self._calculate_mftoffset()
                f.seek(self.mftoffset)
                f.seek(number * 1024, 1)
                return array.array("B", f.read(1024))

    def mft_get_record(self, number):
        buf = self.mft_get_record_buf(number)
        if buf == array.array("B", ""):
            raise InvalidMFTRecordNumber(number)
        return MFTRecord(buf, 0, False)

    # memoization is key here.
    @memoize(100, keyfunc=lambda r, _:
             str(r.magic()) + str(r.lsn()) + str(r.link_count()) + \
             str(r.mft_record_number()) + str(r.flags()))
    def mft_record_build_path(self, record, cycledetector=None):
        if cycledetector is None:
            cycledetector = {}
        rec_num = record.mft_record_number() & 0xFFFFFFFFFFFF
        if record.mft_record_number() & 0xFFFFFFFFFFFF == 0x0005:
            if self.prefix:
                return self.prefix
            else:
                return "\\."
        fn = record.filename_information()
        if not fn:
            return "\\??"
        parent_record_num = fn.mft_parent_reference() & 0xFFFFFFFFFFFF
        parent_buf = self.mft_get_record_buf(parent_record_num)
        if parent_buf == array.array("B", ""):
            return "\\??\\" + fn.filename()
        parent = MFTRecord(parent_buf, 0, False)
        if parent.sequence_number() != fn.mft_parent_reference() >> 48:
            return "\\$OrphanFiles\\" + fn.filename()
        if rec_num in cycledetector:
            debug("Cycle detected")
            if self.prefix:
                return self.prefix + "\\<CYCLE>"
            else:
                return "\\<CYCLE>"
        cycledetector[rec_num] = True
        return self.mft_record_build_path(parent, cycledetector) + "\\" + fn.filename()

    def mft_get_record_by_path(self, path):
        # TODO could optimize here by trying to use INDX buffers
        # and actually walk through the FS
        count = -1
        for record in self.record_generator():
            count += 1
            if record.magic() != 0x454C4946:
                continue
            if not record.is_active():
                continue
            record_path = self.mft_record_build_path(record, {})
            if record_path.lower() != path.lower():
                continue
            return record
        return False

    def read(self, offset, length):
        if self.filetype == "image":
            with open(self.filename, "rb") as f:
                f.seek(offset)
                return array.array("B", f.read(length))
        return array.array("B", "")
