# emacs: -*- mode: python; py-indent-offset: 4; tab-width: 4; indent-tabs-mode: nil -*-
# ex: set sts=4 ts=4 sw=4 noet:
# ## ### ### ### ### ### ### ### ### ### ### ### ### ### ### ### ### ### ### ##
#
#   See COPYING file distributed along with the datalad package for the
#   copyright and license terms.
#
# ## ### ### ### ### ### ### ### ### ### ### ### ### ### ### ### ### ### ### ##
"""BIDS metadata parser (http://bids.neuroimaging.io)"""

from __future__ import absolute_import
# use pybids to evolve with the standard without having to track it too much
from bids.grabbids import BIDSLayout
import re
import csv
from io import open
from os.path import join as opj
from os.path import exists
from datalad.dochelpers import exc_str
from datalad.metadata.parsers.base import BaseMetadataParser
from datalad.metadata.definitions import vocabulary_id
from datalad.utils import open_r_encdetect

from datalad import cfg

import logging
lgr = logging.getLogger('datalad.metadata.parser.bids')


vocabulary = {
    # characteristics (metadata keys)
    "bids:age(years)": {
        '@id': "pato:0000011",
        'unit': "uo:0000036",
        'unit_label': "year",
        'description': "age of a sample (organism) at the time of data acquisition in years"},
}

## only BIDS metadata properties that match a key in this dict will be considered
## for reporting, the rest becomes 'comment<orig>'
content_metakey_map = {
    'participant_id': 'bids:participant_id',
    'age': 'bids:age(years)',
}

sex_label_map = {
    'f': 'female',
    'm': 'male',
}


class MetadataParser(BaseMetadataParser):
    _dsdescr_fname = 'dataset_description.json'

    _key2stdkey = {
        'Name': 'name',
        'License': 'license',
        'Authors': 'author',
        'ReferencesAndLinks': 'citation',
        'Funding': 'fundedby',
        'Description': 'description',
    }

    def get_metadata(self, dataset, content):
        # (I think) we need a cheap test to see if there is anything, otherwise
        # pybids we try to parse any size of directory hierarchy in full
        if not exists(opj(self.ds.path, self._dsdescr_fname)):
            return {}, []

        bids = BIDSLayout(self.ds.path)
        dsmeta = self._get_dsmeta(bids)

        if not content:
            return dsmeta, []

        return dsmeta, self._get_cnmeta(bids)

    def _get_dsmeta(self, bids):
        context = {}
        meta = {self._key2stdkey.get(k, 'comment<{}>'.format(k)): v
                for k, v in bids.get_metadata(
                    opj(self.ds.path, self._dsdescr_fname)).items()}

        # TODO maybe normalize labels of standard licenses to definition URIs
        # perform mapping

        README_fname = opj(self.ds.path, 'README')
        if not meta.get('description') and exists(README_fname):
            # BIDS uses README to provide description, so if was not
            # explicitly provided to possibly override longer README, let's just
            # load README
            desc = open_r_encdetect(README_fname).read()
            meta['description'] = desc.strip()

        # special case
        # Could be None which we can't strip so or ''
        bids_version = (meta.get('comment<BIDSVersion>', '') or '').strip()
        bids_defurl = 'http://bids.neuroimaging.io'
        if bids_version:
            bids_defurl += '/bids_spec{}.pdf'.format(bids_version)
        meta['conformsto'] = bids_defurl
        context['bids'] = {
            # not really a working URL, but BIDS doesn't provide term defs in
            # any accessible way
            '@id': '{}#'.format(bids_defurl),
            'description': 'ad-hoc vocabulary for the Brain Imaging Data Structure (BIDS) standard',
            'type': vocabulary_id,
        }
        context.update(vocabulary)
        meta['@context'] = context
        return meta

    def _get_cnmeta(self, bids):
        # TODO any custom handling of participants infos should eventually
        # be done by pybids in one way or another
        path_props = {}
        participants_fname = opj(self.ds.path, 'participants.tsv')
        if exists(participants_fname):
            try:
                for rx, info in yield_participant_info(participants_fname):
                    path_props[rx] = info
            except Exception as exc:
                lgr.warning(
                    "Failed to load participants info due to: %s. Skipping the rest of file",
                    exc_str(exc)
                )

        # now go over all files in the dataset and query pybids for its take
        # on each of them
        for f in self.paths:
            # BIDS carries a substantial portion of its metadata in JSON
            # sidecar files. we ignore them here completely
            # this might yield some false-negatives in theory, but
            # this case has not been observed in practice yet, hence
            # doing it cheap for now
            if f.endswith('.json'):
                continue
            md = {}
            try:
                md.update(
                    {'bids:{}'.format(k): v
                     for k, v in bids.get_metadata(opj(self.ds.path, f)).items()
                     # no nested structures for now (can be monstrous when DICOM
                     # metadata is embedded)
                     if not isinstance(v, dict)})
            except Exception as e:
                lgr.debug('no usable BIDS metadata for %s in %s: %s',
                          f, self.ds, exc_str(e))
                if cfg.get('datalad.runtime.raiseonerror'):
                    raise

            # no check al props from other sources and apply them
            for rx in path_props:
                if rx.match(f):
                    md.update(path_props[rx])
            yield f, md


def yield_participant_info(fname):
    with open(fname) as tsvfile:
        # add robustness, use a sniffer
        try:
            dialect = csv.Sniffer().sniff(tsvfile.read(16384))
        except:
            lgr.warning('Could not determine file-format, assuming TSV')
            dialect = 'excel-tab'
        tsvfile.seek(0)
        for row in csv.DictReader(tsvfile, dialect=dialect):
            if 'participant_id' not in row:
                # not sure what this is, but we cannot use it
                break
            props = {}
            for k in row:
                # take away some ambiguity
                normk = k.lower()
                hk = content_metakey_map.get(normk, None)
                val = row[k]
                if hk is None:
                    hk = 'comment<participant#{}>'.format(normk)
                if hk in ('comment<participant#sex>', 'comment<participant#gender>'):
                    val = sex_label_map.get(row[k].lower(), row[k].lower())
                if val:
                    props[hk] = val
            if props:
                yield re.compile(r'^{}/.*'.format(row['participant_id'])), props
