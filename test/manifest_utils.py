from xml.dom.minidom import parseString
import http_utils
import struct
import base64
import math

def getAttributesDict(node):
	result = {}
	for attIndex in xrange(len(node.attributes)):
		curAtt = node.attributes.item(attIndex)
		result[curAtt.name] = curAtt.value
	return result

def getAbsoluteUrl(url, baseUrl = ''):
	if not url.startswith('http://') and not url.startswith('https://'):
		if baseUrl == '':
			raise Exception('bad url %s' % url)
		if baseUrl.endswith('/'):
			url = baseUrl + url
		else:
			url = baseUrl + '/' + url
	return url
	
def getHlsMediaPlaylistUrls(baseUrl, urlContent):
	result = []
	for curLine in urlContent.split('\n'):
		curLine = curLine.strip()
		if len(curLine) == 0:
			continue
		if curLine[0] == '#':
			spilttedLine = curLine.split('URI="', 1)
			if len(spilttedLine) < 2:
				continue
			result.append(getAbsoluteUrl(spilttedLine[1].split('"')[0], baseUrl))
			continue
		result.append(getAbsoluteUrl(curLine, baseUrl))
	return result
	
def getHlsMasterPlaylistUrls(baseUrl, urlContent, headers):
	result = []
	for curLine in urlContent.split('\n'):
		curLine = curLine.strip()
		if len(curLine) == 0:
			continue
		# get the current url
		if curLine[0] == '#':
			if not 'URI="' in curLine:
				continue
			curUrl = curLine.split('URI="')[1].split('"')[0]
		else:
			curUrl = curLine
		curUrl = getAbsoluteUrl(curUrl, baseUrl)
		result.append(curUrl)
		
		# get the segments of the current url
		code, _, mediaContent = http_utils.getUrl(curUrl, headers)
		if code != 200 or len(mediaContent) == 0:
			continue
		curBaseUrl = curUrl.rsplit('/', 1)[0]
		result += getHlsMediaPlaylistUrls(curBaseUrl, mediaContent)
	return result

def getDashManifestUrls(baseUrl, urlContent, headers):
	parsed = parseString(urlContent)

	# try SegmentList
	urls = set([])
	for node in parsed.getElementsByTagName('SegmentList'):
		for childNode in node.getElementsByTagName('SegmentURL'):
			atts = getAttributesDict(childNode)
			urls.add(atts['media'])
		for childNode in node.getElementsByTagName('Initialization'):
			atts = getAttributesDict(childNode)
			urls.add(atts['sourceURL'])
	if len(urls) > 0:
		return map(lambda x: getAbsoluteUrl(x, baseUrl), urls)
	
	# try SegmentTemplate - get media duration
	mediaDuration = None
	for node in parsed.getElementsByTagName('MPD'):
		atts = getAttributesDict(node)
		mediaDuration = float(atts['mediaPresentationDuration'][2:-1])
	
	# get the url templates and segment duration
	result = []
	for base in parsed.getElementsByTagName('AdaptationSet'):
	
		segmentDuration = None
		for node in base.getElementsByTagName('SegmentTemplate'):
			atts = getAttributesDict(node)
			urls.add(atts['media'])
			urls.add(atts['initialization'])
			if atts.has_key('duration'):
				segmentDuration = int(atts['duration'])

		# get the representation ids
		repIds = set([])
		for node in base.getElementsByTagName('Representation'):
			atts = getAttributesDict(node)
			repIds.add(atts['id'])
			
		# get the segment count from SegmentTimeline
		segmentCount = None
		for node in base.getElementsByTagName('SegmentTimeline'):
			segmentCount = 0
			for childNode in node.childNodes:
				if childNode.nodeType == node.ELEMENT_NODE and childNode.nodeName == 'S':
					atts = getAttributesDict(childNode)
					if atts.has_key('r'):
						segmentCount += int(atts['r'])
					segmentCount += 1

		if segmentCount == None:
			if segmentDuration == None:
				for curBaseUrl in base.getElementsByTagName('BaseURL'):
					result.append(getAbsoluteUrl(curBaseUrl.firstChild.nodeValue))
				continue
			segmentCount = int(math.ceil(mediaDuration * 1000 / segmentDuration))
		
		for url in urls:
			for curSeg in xrange(segmentCount):
				for repId in repIds:
					result.append(getAbsoluteUrl(url.replace('$Number$', '%s' % (curSeg + 1)).replace('$RepresentationID$', repId)))
	return result

def getHdsSegmentIndexes(data):
    result = []
    mediaTime = struct.unpack('>Q', data[0x15:0x1d])[0]
    curPos = 0x5a
    prevDuration = 0
    while curPos < len(data):
        index, timestamp, duration = struct.unpack('>LQL', data[curPos:(curPos + 0x10)])

        curPos += 0x10
        if duration == 0:
            curPos += 1

        if prevDuration != 0:
            if (index, timestamp, duration) == (0, 0, 0):
                repeatCount = (mediaTime - prevTimestamp) / prevDuration
            else:
                repeatCount = index - prevIndex
            result += range(prevIndex, prevIndex + repeatCount)
        (prevIndex, prevTimestamp, prevDuration) = (index, timestamp, duration)
    return result
	
def getHdsManifestUrls(baseUrl, urlContent, headers):
	result = []
	
	# parse the xml
	parsed = parseString(urlContent)
	
	# get the bootstraps
	segmentIndexes = {}
	for node in parsed.getElementsByTagName('bootstrapInfo'):
		atts = getAttributesDict(node)
		if atts.has_key('url'):
			curUrl = getAbsoluteUrl(atts['url'], baseUrl)
			result.append(curUrl)
			
			# get the bootstrap info
			code, _, bootstrapInfo = http_utils.getUrl(curUrl, headers)
			if code != 200 or len(bootstrapInfo) == 0:
				continue
		else:
			bootstrapInfo = base64.b64decode(node.firstChild.nodeValue)
		bootstrapId = atts['id']
		segmentIndexes[bootstrapId] = getHdsSegmentIndexes(bootstrapInfo)
	
	# add the media urls
	for node in parsed.getElementsByTagName('media'):
		atts = getAttributesDict(node)
		bootstrapId = atts['bootstrapInfoId']
		if not segmentIndexes.has_key(bootstrapId):
			continue
		
		url = atts['url']
		for curSeg in segmentIndexes[bootstrapId]:
			result.append(getAbsoluteUrl('%s/%sSeg1-Frag%s' % (baseUrl, url, curSeg)))

	return result

def getMssManifestUrls(baseUrl, urlContent, headers):
	result = []
	parsed = parseString(urlContent)
	for node in parsed.getElementsByTagName('StreamIndex'):		
		# get the bitrates
		bitrates = set([])
		for childNode in node.getElementsByTagName('QualityLevel'):
			bitrates.add(getAttributesDict(childNode)['Bitrate'])
		
		# get the timestamps
		timestamps = []
		curTimestamp = 0
		for childNode in node.getElementsByTagName('c'):
			curAtts = getAttributesDict(childNode)
			if curAtts.has_key('t'):
				curTimestamp = int(curAtts['t'])
			duration = int(curAtts['d'])
			timestamps.append('%s' % curTimestamp)
			curTimestamp += duration
			
		# build the final urls
		atts = getAttributesDict(node)
		url = atts['Url']
		for bitrate in bitrates:
			for timestamp in timestamps:
				result.append(getAbsoluteUrl('%s/%s' % (baseUrl, url.replace('{bitrate}', bitrate).replace('{start time}', timestamp))))
	return result

PARSER_BY_MIME_TYPE = {
	'application/dash+xml': getDashManifestUrls,
	'video/f4m': getHdsManifestUrls,
	'application/vnd.apple.mpegurl': getHlsMasterPlaylistUrls,
	'text/xml': getMssManifestUrls,
}

def getManifestUrls(baseUrl, urlContent, mimeType, headers):
	if not PARSER_BY_MIME_TYPE.has_key(mimeType):
		return []
	return PARSER_BY_MIME_TYPE[mimeType](baseUrl, urlContent, headers)
