# -*- coding: utf-8 -*-
"""Simulated WFS 2.0 server for the tests (no network).

Understands GetFeature with typeNames, count, startIndex, resultType=hits,
bbox=x0,y0,x1,y1,crs and resourceId=… — the way source_downloader queries the
services. With unstable=True, paging behaves like the M-V service:
the same page returns different objects on every call."""
import random
import hashlib
import urllib.parse

NS = ('xmlns:wfs="http://www.opengis.net/wfs/2.0" xmlns:gml="http://www.opengis.net/gml/3.2" '
      'xmlns:ad="http://inspire.ec.europa.eu/schemas/ad/4.0" '
      'xmlns:gn="http://inspire.ec.europa.eu/schemas/gn/4.0" xmlns:xlink="http://www.w3.org/1999/xlink"')


def _name_xml(tag, gid, text, level=None):
    lvl = (f'<ad:level xlink:href="http://inspire.ec.europa.eu/codelist/AdministrativeHierarchyLevel/{level}"/>'
           if level else '')
    return (f'<ad:{tag} gml:id="{gid}"><ad:name><gn:GeographicalName><gn:spelling><gn:SpellingOfName>'
            f'<gn:text>{text}</gn:text></gn:SpellingOfName></gn:spelling></gn:GeographicalName></ad:name>'
            f'{lvl}</ad:{tag}>')


def _address_xml(gid, hn, x, y, refs, base):
    comps = ''.join(
        f'<ad:component xlink:href="{base}?REQUEST=GetFeature&amp;ID={r}#{r}"/>' for r in refs)
    return (f'<ad:Address gml:id="{gid}"><ad:position><ad:GeographicPosition><ad:geometry>'
            f'<gml:Point gml:id="P{gid}" srsName="urn:ogc:def:crs:EPSG::25833"><gml:pos>{x} {y}</gml:pos>'
            f'</gml:Point></ad:geometry></ad:GeographicPosition></ad:position>'
            f'<ad:locator><ad:AddressLocator><ad:designator><ad:LocatorDesignator>'
            f'<ad:designator>{hn}</ad:designator><ad:type xlink:href="http://inspire.ec.europa.eu/'
            f'codelist/LocatorDesignatorTypeValue/addressNumber"/></ad:LocatorDesignator></ad:designator>'
            f'</ad:AddressLocator></ad:locator>{comps}</ad:Address>')


class FakeWfs:
    def __init__(self, n_addresses=50, n_streets=12, max_count=10, unstable=False,
                 base='https://wfs/x', seed=1):
        self.base, self.max_count, self.unstable = base, max_count, unstable
        self.rnd = random.Random(seed)
        self.requests = []
        self.features = {'ad:AdminUnitName': [], 'ad:ThoroughfareName': [], 'ad:Address': []}
        self.features['ad:AdminUnitName'] = [
            ('AdminUnitName_L', None, _name_xml('AdminUnitName', 'AdminUnitName_L', 'Testland', '2ndOrder')),
            ('AdminUnitName_G', None, _name_xml('AdminUnitName', 'AdminUnitName_G', 'Teststadt', '5thOrder'))]
        for s in range(n_streets):
            gid = f'ThoroughfareName_{s:04d}'
            self.features['ad:ThoroughfareName'].append(
                (gid, None, _name_xml('ThoroughfareName', gid, f'Teststraße {s}')))
        for i in range(n_addresses):
            gid = f'Address_{i:05d}'
            # Grid 0..1000 m; some points exactly on tile boundaries (x=500)
            x = 500.0 if i % 7 == 0 else float((i * 37) % 1000)
            y = float((i * 53) % 1000)
            refs = ['AdminUnitName_L', 'AdminUnitName_G', f'ThoroughfareName_{i % n_streets:04d}']
            self.features['ad:Address'].append(
                (gid, (x, y), _address_xml(gid, str(i + 1), 300000 + x, 5900000 + y, refs, base)))

    # -- Build response -------------------------------------------------------
    def _collection(self, items):
        members = ''.join(f'<wfs:member>{xml}</wfs:member>' for _gid, _xy, xml in items)
        return (f'<wfs:FeatureCollection {NS} numberReturned="0">{members}'
                f'</wfs:FeatureCollection>').encode('utf-8')

    def _respond(self, url):
        self.requests.append(url)
        q = dict(urllib.parse.parse_qsl(urllib.parse.urlparse(url).query))
        if 'resourceId' in q:
            wanted = set(q['resourceId'].split(','))
            items = [f for lst in self.features.values() for f in lst if f[0] in wanted]
            return self._collection(items)
        typ = q.get('typeNames')
        items = list(self.features.get(typ, []))
        if 'bbox' in q:
            x0, y0, x1, y1 = (float(v) for v in q['bbox'].split(',')[:4])
            items = [f for f in items if f[1] and x0 <= f[1][0] <= x1 and y0 <= f[1][1] <= y1]
        if q.get('resultType') == 'hits':
            return f'<wfs:FeatureCollection {NS} numberMatched="{len(items)}"/>'.encode()
        count = min(int(q.get('count', self.max_count)), self.max_count)
        start = int(q.get('startIndex', 0))
        if self.unstable and 'bbox' not in q:
            self.rnd.shuffle(items)          # like M-V: no stable order
        return self._collection(items[start:start + count])

    # -- Http interface of source_downloader ----------------------------------
    def get(self, url, headers=None, max_bytes=None):
        return 200, {}, self._respond(url)

    def download(self, url, dest, headers=None, progress=None, cancel=None):
        body = self._respond(url)
        with open(dest, 'wb') as f:
            f.write(body)
        return 200, {'_sha256': hashlib.sha256(body).hexdigest(), '_size': str(len(body))}
