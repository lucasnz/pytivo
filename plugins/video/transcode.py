import logging
import math
import os
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import threading
import time

import lrucache

import config
import metadata

logger = logging.getLogger('pyTivo.video.transcode')

info_cache = lrucache.LRUCache(1000)
ffmpeg_procs = {}
reapers = {}

GOOD_MPEG_FPS = ['23.98', '24.00', '25.00', '29.97',
                 '30.00', '50.00', '59.94', '60.00']

BLOCKSIZE = 512 * 1024
MAXBLOCKS = 2
TIMEOUT = 600

# XXX BIG HACK
# subprocess is broken for me on windows so super hack
def patchSubprocess():
    o = subprocess.Popen._make_inheritable

    def _make_inheritable(self, handle):
        if not handle: return subprocess.GetCurrentProcess()
        return o(self, handle)

    subprocess.Popen._make_inheritable = _make_inheritable
mswindows = (sys.platform == "win32")
if mswindows:
    patchSubprocess()

def debug(msg):
    if type(msg) == str:
        try:
            msg = msg.decode('utf8')
        except:
            if sys.platform == 'darwin':
                msg = msg.decode('macroman')
            else:
                msg = msg.decode('cp1252')
    logger.debug(msg)

def transcode(isQuery, inFile, outFile, tsn='', mime='', thead=''):
    vcodec = select_videocodec(inFile, tsn, mime)

    settings = select_buffsize(tsn) + vcodec
    if not vcodec[1] == 'copy':
        settings += (select_videobr(inFile, tsn) +
                     select_maxvideobr(tsn) +
                     select_videofps(inFile, tsn) +
                     select_aspect(inFile, tsn))

    acodec = select_audiocodec(isQuery, inFile, tsn)
    settings += acodec
    if not acodec[1] == 'copy':
        settings += (select_audiobr(tsn) +
                     select_audiofr(inFile, tsn) +
                     select_audioch(inFile, tsn))

    settings += [select_audiolang(inFile, tsn),
                 select_ffmpegprams(tsn)]

    settings += select_format(tsn, mime)

    settings = ' '.join(settings).split()
    vfilter = select_videofilter(inFile)
    if vfilter:
        settings = vfilter + settings

    if isQuery:
        return settings

    ffmpeg_path = config.get_bin('ffmpeg')

    fname = unicode(inFile, 'utf-8')
    if mswindows:
        fname = fname.encode('cp1252')

    if inFile[-5:].lower() == '.tivo':
        tivodecode_path = config.get_bin('tivodecode')
        tivo_mak = config.get_server('tivo_mak')
        tcmd = [tivodecode_path, '-m', tivo_mak, fname]
        tivodecode = subprocess.Popen(tcmd, stdout=subprocess.PIPE,
                                      bufsize=(512 * 1024))
        if tivo_compatible(inFile, tsn)[0]:
            cmd = ''
            ffmpeg = tivodecode
        else:
            cmd = [ffmpeg_path, '-i', '-'] + settings
            ffmpeg = subprocess.Popen(cmd, stdin=tivodecode.stdout,
                                      stdout=subprocess.PIPE,
                                      bufsize=(512 * 1024))
    else:
        cmd = [ffmpeg_path, '-i', fname] + settings
        ffmpeg = subprocess.Popen(cmd, bufsize=(512 * 1024),
                                  stdout=subprocess.PIPE)

    if cmd:
        debug('transcoding to tivo model ' + tsn[:3] + ' using ffmpeg command:')
        debug(' '.join(cmd))

    ffmpeg_procs[inFile] = {'process': ffmpeg, 'start': 0, 'end': 0,
                            'last_read': time.time(), 'blocks': []}
    if thead:
        ffmpeg_procs[inFile]['blocks'].append(thead)
    reap_process(inFile)
    return resume_transfer(inFile, outFile, 0)

def is_resumable(inFile, offset):
    if inFile in ffmpeg_procs:
        proc = ffmpeg_procs[inFile]
        if proc['start'] <= offset < proc['end']:
            return True
        else:
            cleanup(inFile)
            kill(proc['process'])
    return False

def resume_transfer(inFile, outFile, offset):
    proc = ffmpeg_procs[inFile]
    offset -= proc['start']
    count = 0

    try:
        for block in proc['blocks']:
            length = len(block)
            if offset < length:
                if offset > 0:
                    block = block[offset:]
                outFile.write('%x\r\n' % len(block))
                outFile.write(block)
                outFile.write('\r\n')
                count += len(block)
            offset -= length
        outFile.flush()
    except Exception, msg:
        logger.info(msg)
        return count

    proc['start'] = proc['end']
    proc['blocks'] = []

    return count + transfer_blocks(inFile, outFile)

def transfer_blocks(inFile, outFile):
    proc = ffmpeg_procs[inFile]
    blocks = proc['blocks']
    count = 0

    while True:
        try:
            block = proc['process'].stdout.read(BLOCKSIZE)
            proc['last_read'] = time.time()
        except Exception, msg:
            logger.info(msg)
            cleanup(inFile)
            kill(proc['process'])
            break

        if not block:
            try:
                outFile.flush()
            except Exception, msg:
                logger.info(msg)
            else:
                cleanup(inFile)
            break

        blocks.append(block)
        proc['end'] += len(block)
        if len(blocks) > MAXBLOCKS:
            proc['start'] += len(blocks[0])
            blocks.pop(0)

        try:
            outFile.write('%x\r\n' % len(block))
            outFile.write(block)
            outFile.write('\r\n')
            count += len(block)
        except Exception, msg:
            logger.info(msg)
            break

    return count

def reap_process(inFile):
    if ffmpeg_procs and inFile in ffmpeg_procs:
        proc = ffmpeg_procs[inFile]
        if proc['last_read'] + TIMEOUT < time.time():
            del ffmpeg_procs[inFile]
            del reapers[inFile]
            kill(proc['process'])
        else:
            reaper = threading.Timer(TIMEOUT, reap_process, (inFile,))
            reapers[inFile] = reaper
            reaper.start()

def cleanup(inFile):
    del ffmpeg_procs[inFile]
    reapers[inFile].cancel()
    del reapers[inFile]

def select_audiocodec(isQuery, inFile, tsn='', mime=''):
    if inFile[-5:].lower() == '.tivo':
        return ['-c:a', 'copy']
    vInfo = video_info(inFile)
    codectype = vInfo['vCodec']
    # Default, compatible with all TiVo's
    codec = 'ac3'
    if mime == 'video/mp4':
        compatiblecodecs = ('mpeg4aac', 'libfaad', 'mp4a', 'aac',
                            'ac3', 'liba52')
    else:
        compatiblecodecs = ('ac3', 'liba52', 'mp2')

    if vInfo['aCodec'] in compatiblecodecs:
        aKbps = vInfo['aKbps']
        aCh = vInfo['aCh']
        if aKbps == None:
            if vInfo['aCodec'] in ('mpeg4aac', 'libfaad', 'mp4a', 'aac'):
                # along with the channel check below this should
                # pass any AAC audio that has undefined 'aKbps' and
                # is <= 2 channels.  Should be TiVo compatible.
                codec = 'copy'
            elif not isQuery:
                vInfoQuery = audio_check(inFile, tsn)
                if vInfoQuery == None:
                    aKbps = None
                    aCh = None
                else:
                    aKbps = vInfoQuery['aKbps']
                    aCh = vInfoQuery['aCh']
            else:
                codec = 'TBA'
        if aKbps and int(aKbps) <= config.getMaxAudioBR(tsn):
            # compatible codec and bitrate, do not reencode audio
            codec = 'copy'
        if vInfo['aCodec'] != 'ac3' and (aCh == None or aCh > 2):
            codec = 'ac3'
    val = ['-c:a', codec]
    if not (codec == 'copy' and codectype == 'mpeg2video'):
        val.append('-copyts')
    return val

def select_audiofr(inFile, tsn):
    freq = '48000'  # default
    vInfo = video_info(inFile)
    if vInfo['aFreq'] == '44100':
        # compatible frequency
        freq = vInfo['aFreq']
    return ['-ar', freq]

def select_audioch(inFile, tsn):
    # AC-3 max channels is 5.1
    if video_info(inFile)['aCh'] > 6:
        debug('Too many audio channels for AC-3, using 5.1 instead')
        return ['-ac', '6']
    return []

def select_audiolang(inFile, tsn):
    vInfo = video_info(inFile)
    audio_lang = config.get_tsn('audio_lang', tsn)
    debug('audio_lang: %s' % audio_lang)
    if vInfo['mapAudio']:
        # default to first detected audio stream to begin with
        stream = vInfo['mapAudio'][0][0]
        debug('set first detected audio stream by default: %s' % stream)
    if audio_lang != None and vInfo['mapVideo'] != None:
        langmatch_curr = []
        langmatch_prev = vInfo['mapAudio'][:]
        for lang in audio_lang.replace(' ', '').lower().split(','):
            debug('matching lang: %s' % lang)
            for s, l in langmatch_prev:
                if lang in s + l.replace(' ', '').lower():
                    debug('matched: %s' % s + l.replace(' ', '').lower())
                    langmatch_curr.append((s, l))
            # if only 1 item matched we're done
            if len(langmatch_curr) == 1:
                stream = langmatch_curr[0][0]
                debug('found exactly one match: %s' % stream)
                break
            # if more than 1 item matched copy the curr area to the prev
            # array we only need to look at the new shorter list from
            # now on
            elif len(langmatch_curr) > 1:
                langmatch_prev = langmatch_curr[:]
                # default to the first item matched thus far
                stream = langmatch_curr[0][0]
                debug('remember first match: %s' % stream)
                langmatch_curr = []
    # don't let FFmpeg auto select audio stream, pyTivo defaults to
    # first detected
    if stream:
        debug('selected audio stream: %s' % stream)
        return '-map ' + vInfo['mapVideo'] + ' -map ' + stream
    # if no audio is found
    debug('selected audio stream: None detected')
    return ''

def select_videofps(inFile, tsn):
    vInfo = video_info(inFile)
    fps = ['-r', '29.97']  # default
    if config.isHDtivo(tsn) and vInfo['vFps'] in GOOD_MPEG_FPS:
        fps = []
    return fps

def select_videocodec(inFile, tsn, mime=''):
    codec = ['-c:v']
    vInfo = video_info(inFile)
    if tivo_compatible_video(vInfo, tsn, mime)[0]:
        codec.append('copy')
        if (mime == 'video/x-tivo-mpeg-ts'):
            org_codec = vInfo.get('vCodec', '')
            if org_codec == 'h264':
                codec += ['-bsf', 'h264_mp4toannexb']
            elif org_codec == 'hevc':
                codec += ['-bsf', 'hevc_mp4toannexb']
    else:
        codec += ['mpeg2video', '-pix_fmt', 'yuv420p']  # default
    return codec

def select_videofilter(inFile):
    vInfo = video_info(inFile)
    #legacy subtitle support to be removed
    subtitles = vInfo.get('subtitles')
    if subtitles:
        return ['-vf', subtitles]

    subfile = vInfo.get('subFile')

    #first select a subFile in the metadata.txt file
    #then select the subFile with the same filename as the video file (video file: video.mpg, subFile: video.mpg.srt)
    #else if the video has embedded subtitles, use them
    if subfile and os.path.exists(os.path.join(os.path.split(inFile)[0], subfile)):
        subfile = os.path.join(os.path.split(inFile)[0], subfile)
    elif os.path.exists(os.path.join(os.path.split(inFile)[0], os.path.basename(inFile) + '.srt')):
        subfile = os.path.join(os.path.split(inFile)[0], os.path.basename(inFile) + '.srt')
    elif os.path.exists(os.path.join(os.path.split(inFile)[0], os.path.basename(inFile) + '.ass')):
        subfile = os.path.join(os.path.split(inFile)[0], os.path.basename(inFile) + '.ass')
    elif vInfo.get('subType'):
        #TODO need to look at handling when there is more than one subtitle track
        subfile = inFile

    if subfile:
        if subfile == inFile:
            subType = vInfo.get('subType')
        else:
            sInfo = video_info(subfile)
            subType = sInfo.get('subType')
            logger.info('sInfo: %s' % sInfo)

        #escape ffmpeg special characters
        #not sure how to escape ' - currently can't support files with this character in the path
        subfile_escape = re.sub(r'([\\\[\]\:@;,])', r'\\\1', subfile)

        if subType == 'subrip':
            vfilter = ['-vf', 'subtitles=\\\'%s\\\'' % subfile_escape]
            logger.info('video filter: %s' % vfilter)
            return vfilter
        elif subType == 'ass':
            vfilter = ['-vf', 'ass=\\\'%s\\\'' % subfile_escape]
            logger.info('video filter: %s' % vfilter)
            return vfilter

    return False

def select_videobr(inFile, tsn, mime=''):
    return ['-b:v', str(select_videostr(inFile, tsn, mime) / 1000) + 'k']

def select_videostr(inFile, tsn, mime=''):
    vInfo = video_info(inFile)
    if tivo_compatible_video(vInfo, tsn, mime)[0]:
        video_str = int(vInfo['kbps'])
        if vInfo['aKbps']:
            video_str -= int(vInfo['aKbps'])
        video_str *= 1000
    else:
        video_str = config.strtod(config.getVideoBR(tsn))
        if config.isHDtivo(tsn) and vInfo['kbps']:
            video_str = max(video_str, int(vInfo['kbps']) * 1000)
        video_str = int(min(config.strtod(config.getMaxVideoBR(tsn)) * 0.95,
                            video_str))
    return video_str

def select_audiobr(tsn):
    return ['-b:a', config.getAudioBR(tsn)]

def select_maxvideobr(tsn):
    return ['-maxrate', config.getMaxVideoBR(tsn)]

def select_buffsize(tsn):
    return ['-bufsize', config.getBuffSize(tsn)]

def select_ffmpegprams(tsn):
    params = config.getFFmpegPrams(tsn)
    if not params:
        params = ''
    return params

def select_format(tsn, mime):
    if mime == 'video/x-tivo-mpeg-ts':
        fmt = 'mpegts'
    else:
        fmt = 'vob'
    return ['-f', fmt, '-']

def pad_TB(TIVO_WIDTH, TIVO_HEIGHT, multiplier, vInfo):
    endHeight = int(((TIVO_WIDTH * vInfo['vHeight']) /
                      vInfo['vWidth']) * multiplier)
    if endHeight % 2:
        endHeight -= 1
    topPadding = (TIVO_HEIGHT - endHeight) / 2
    if topPadding % 2:
        topPadding -= 1
    return ['-vf', 'scale=%d:%d,pad=%d:%d:0:%d' % (TIVO_WIDTH,
            endHeight, TIVO_WIDTH, TIVO_HEIGHT, topPadding)]

def pad_LR(TIVO_WIDTH, TIVO_HEIGHT, multiplier, vInfo):
    endWidth = int((TIVO_HEIGHT * vInfo['vWidth']) /
                   (vInfo['vHeight'] * multiplier))
    if endWidth % 2:
        endWidth -= 1
    leftPadding = (TIVO_WIDTH - endWidth) / 2
    if leftPadding % 2:
        leftPadding -= 1
    return ['-vf', 'scale=%d:%d,pad=%d:%d:%d:0' % (endWidth,
            TIVO_HEIGHT, TIVO_WIDTH, TIVO_HEIGHT, leftPadding)]

def select_aspect(inFile, tsn = ''):
    TIVO_WIDTH = config.getTivoWidth(tsn)
    TIVO_HEIGHT = config.getTivoHeight(tsn)

    vInfo = video_info(inFile)

    debug('tsn: %s' % tsn)

    aspect169 = config.get169Setting(tsn)

    debug('aspect169: %s' % aspect169)

    optres = config.getOptres(tsn)

    debug('optres: %s' % optres)

    if optres:
        optHeight = config.nearestTivoHeight(vInfo['vHeight'])
        optWidth = config.nearestTivoWidth(vInfo['vWidth'])
        if optHeight < TIVO_HEIGHT:
            TIVO_HEIGHT = optHeight
        if optWidth < TIVO_WIDTH:
            TIVO_WIDTH = optWidth

    if vInfo.get('par2'):
        par2 = vInfo['par2']
    elif vInfo.get('par'):
        par2 = float(vInfo['par'])
    else:
        # Assume PAR = 1.0
        par2 = 1.0

    debug(('File=%s vCodec=%s vWidth=%s vHeight=%s vFps=%s millisecs=%s ' +
           'TIVO_HEIGHT=%s TIVO_WIDTH=%s') % (inFile, vInfo['vCodec'],
          vInfo['vWidth'], vInfo['vHeight'], vInfo['vFps'],
          vInfo['millisecs'], TIVO_HEIGHT, TIVO_WIDTH))

    if config.isHDtivo(tsn) and not optres:
        if vInfo['par']:
            npar = par2

            # adjust for pixel aspect ratio, if set

            if npar < 1.0:
                return ['-s', '%dx%d' % (vInfo['vWidth'],
                                         math.ceil(vInfo['vHeight'] / npar))]
            elif npar > 1.0:
                # FFMPEG expects width to be a multiple of two
                return ['-s', '%dx%d' % (math.ceil(vInfo['vWidth']*npar/2.0)*2,
                                         vInfo['vHeight'])]

        if vInfo['vHeight'] <= TIVO_HEIGHT:
            # pass all resolutions to S3, except heights greater than
            # conf height
            return []
        # else, resize video.

    d = gcd(vInfo['vHeight'], vInfo['vWidth'])
    rheight, rwidth = vInfo['vHeight'] / d, vInfo['vWidth'] / d
    debug('rheight=%s rwidth=%s' % (rheight, rwidth))

    if (rwidth, rheight) in [(1, 1)] and vInfo['par1'] == '8:9':
        debug('File + PAR is within 4:3.')
        return ['-aspect', '4:3', '-s', '%sx%s' % (TIVO_WIDTH, TIVO_HEIGHT)]

    elif ((rwidth, rheight) in [(4, 3), (10, 11), (15, 11), (59, 54),
                                (59, 72), (59, 36), (59, 54)] or
          vInfo['dar1'] == '4:3'):
        debug('File is within 4:3 list.')
        return ['-aspect', '4:3', '-s', '%sx%s' % (TIVO_WIDTH, TIVO_HEIGHT)]

    elif (((rwidth, rheight) in [(16, 9), (20, 11), (40, 33), (118, 81),
                                (59, 27)] or vInfo['dar1'] == '16:9')
          and (aspect169 or config.get169Letterbox(tsn))):
        debug('File is within 16:9 list and 16:9 allowed.')

        if config.get169Blacklist(tsn) or (aspect169 and
                                           config.get169Letterbox(tsn)):
            aspect = '4:3'
        else:
            aspect = '16:9'
        return ['-aspect', aspect, '-s', '%sx%s' % (TIVO_WIDTH, TIVO_HEIGHT)]

    else:
        settings = ['-aspect']

        multiplier16by9 = (16.0 * TIVO_HEIGHT) / (9.0 * TIVO_WIDTH) / par2
        multiplier4by3  =  (4.0 * TIVO_HEIGHT) / (3.0 * TIVO_WIDTH) / par2
        ratio = vInfo['vWidth'] * 100 * par2 / vInfo['vHeight']
        debug('par2=%.3f ratio=%.3f mult4by3=%.3f' % (par2, ratio,
                                                      multiplier4by3))

        # If video is wider than 4:3 add top and bottom padding

        if ratio > 133: # Might be 16:9 file, or just need padding on
                        # top and bottom

            if aspect169 and ratio > 135: # If file would fall in 4:3
                                          # assume it is supposed to be 4:3

                if (config.get169Blacklist(tsn) or
                    config.get169Letterbox(tsn)):
                    settings.append('4:3')
                else:
                    settings.append('16:9')

                if ratio > 177: # too short needs padding top and bottom
                    settings += pad_TB(TIVO_WIDTH, TIVO_HEIGHT,
                                       multiplier16by9, vInfo)
                    debug(('16:9 aspect allowed, file is wider ' +
                           'than 16:9 padding top and bottom\n%s') %
                          ' '.join(settings))

                else: # too skinny needs padding on left and right.
                    settings += pad_LR(TIVO_WIDTH, TIVO_HEIGHT,
                                       multiplier16by9, vInfo)
                    debug(('16:9 aspect allowed, file is narrower ' +
                           'than 16:9 padding left and right\n%s') %
                          ' '.join(settings))

            else: # this is a 4:3 file or 16:9 output not allowed
                if ratio > 135 and config.get169Letterbox(tsn):
                    settings.append('16:9')
                    multiplier = multiplier16by9
                else:
                    settings.append('4:3')
                    multiplier = multiplier4by3
                settings += pad_TB(TIVO_WIDTH, TIVO_HEIGHT,
                                   multiplier, vInfo)
                debug(('File is wider than 4:3 padding ' +
                       'top and bottom\n%s') % ' '.join(settings))

        # If video is taller than 4:3 add left and right padding, this
        # is rare. All of these files will always be sent in an aspect
        # ratio of 4:3 since they are so narrow.

        else:
            settings.append('4:3')
            settings += pad_LR(TIVO_WIDTH, TIVO_HEIGHT, multiplier4by3, vInfo)
            debug('File is taller than 4:3 padding left and right\n%s'
                  % ' '.join(settings))

        return settings

def tivo_compatible_video(vInfo, tsn, mime=''):
    message = (True, '')
    while True:
        if select_videofilter(vInfo.get('fname')):
            message = (False, 'Subtitle hard-coding requires reencoding')

            break

        codec = vInfo.get('vCodec', '')
        is4k = config.is4Ktivo(tsn) and codec == 'hevc'
        if mime == 'video/mp4':
            if not (is4k or codec == 'h264'):
                message = (False, 'vCodec %s not compatible' % codec)

            break

        if mime == 'video/bif':
            if codec != 'vc1':
                message = (False, 'vCodec %s not compatible' % codec)

            break

        if mime == 'video/x-tivo-mpeg-ts':
            if not (is4k or codec in ('h264', 'mpeg2video')):
                message = (False, 'vCodec %s not compatible' % codec)

            break

        if codec not in ('mpeg2video', 'mpeg1video'):
            message = (False, 'vCodec %s not compatible' % codec)
            break

        if vInfo['kbps'] != None:
            abit = max('0', vInfo['aKbps'])
            if (int(vInfo['kbps']) - int(abit) >
                config.strtod(config.getMaxVideoBR(tsn)) / 1000):
                message = (False, '%s kbps exceeds max video bitrate' %
                                  vInfo['kbps'])
                break
        else:
            message = (False, '%s kbps not supported' % vInfo['kbps'])
            break

        if config.isHDtivo(tsn):
            # HD Tivo detected, skipping remaining tests.
            break

        if not vInfo['vFps'] in ['29.97', '59.94']:
            message = (False, '%s vFps, should be 29.97' % vInfo['vFps'])
            break

        if ((config.get169Blacklist(tsn) and not config.get169Setting(tsn))
            or (config.get169Letterbox(tsn) and config.get169Setting(tsn))):
            if vInfo['dar1'] and vInfo['dar1'] not in ('4:3', '8:9', '880:657'):
                message = (False, ('DAR %s not supported ' +
                                   'by BLACKLIST_169 tivos') % vInfo['dar1'])
                break

        mode = (vInfo['vWidth'], vInfo['vHeight'])
        if mode not in [(720, 480), (704, 480), (544, 480),
                        (528, 480), (480, 480), (352, 480), (352, 240)]:
            message = (False, '%s x %s not in supported modes' % mode)
        break

    return message

def tivo_compatible_audio(vInfo, inFile, tsn, mime=''):
    message = (True, '')
    while True:
        codec = vInfo.get('aCodec', '')

        if codec == None:
            debug('No audio stream detected')
            break

        if mime == 'video/mp4':
            if codec not in ('mpeg4aac', 'libfaad', 'mp4a', 'aac',
                             'ac3', 'liba52'):
                message = (False, 'aCodec %s not compatible' % codec)
                break
            if vInfo['aCodec'] in ('mpeg4aac', 'libfaad', 'mp4a', 'aac') and (vInfo['aCh'] == None or vInfo['aCh'] > 2):
                message = (False, 'aCodec %s is only supported with 2 or less channels, the track has %s channels' % (codec, vInfo['aCh']))
                break

            audio_lang = config.get_tsn('audio_lang', tsn)
            if audio_lang:
                if vInfo['mapAudio'][0][0] != select_audiolang(inFile, tsn)[-3:]:
                    message = (False, '%s preferred audio track exists' %
                                      audio_lang)
            break

        if mime == 'video/bif':
            if codec != 'wmav2':
                message = (False, 'aCodec %s not compatible' % codec)

            break

        if inFile[-5:].lower() == '.tivo':
            break

        if mime == 'video/x-tivo-mpeg-ts':
            if codec not in ('ac3', 'liba52', 'mp2', 'aac_latm'):
                message = (False, 'aCodec %s not compatible' % codec)

            break

        if codec not in ('ac3', 'liba52', 'mp2'):
            message = (False, 'aCodec %s not compatible' % codec)
            break

        if (not vInfo['aKbps'] or
            int(vInfo['aKbps']) > config.getMaxAudioBR(tsn)):
            message = (False, '%s kbps exceeds max audio bitrate' %
                              vInfo['aKbps'])
            break

        audio_lang = config.get_tsn('audio_lang', tsn)
        if audio_lang:
            if vInfo['mapAudio'][0][0] != select_audiolang(inFile, tsn)[-3:]:
                message = (False, '%s preferred audio track exists' %
                                  audio_lang)
        break

    return message

def tivo_compatible_container(vInfo, inFile, mime=''):
    message = (True, '')
    container = vInfo.get('container', '')
    if ((mime == 'video/mp4' and
         (container != 'mov' or inFile.lower().endswith('.mov'))) or
        (mime == 'video/bif' and container != 'asf') or
        (mime == 'video/x-tivo-mpeg-ts' and container != 'mpegts') or
        (mime in ['video/x-tivo-mpeg', 'video/mpeg', ''] and
         (container != 'mpeg' or vInfo['vCodec'] == 'mpeg1video'))):
        message = (False, 'container %s not compatible' % container)

    return message

def mp4_remuxable(inFile, tsn=''):
    vInfo = video_info(inFile)
    return tivo_compatible_video(vInfo, tsn, 'video/mp4')[0]

def mp4_remux(inFile, basename, tsn='', temp_share_path=''):
    outFile = inFile + '.pyTivo-temp'
    newname = basename + '.pyTivo-temp'

    if temp_share_path:
        newname = os.path.splitext(os.path.split(basename)[1])[0] + '.mp4.pyTivo-temp'
        outFile = os.path.join(temp_share_path, newname)

    if os.path.exists(outFile):
        return None  # ugh!

    ffmpeg_path = config.get_bin('ffmpeg')
    fname = unicode(inFile, 'utf-8')
    oname = unicode(outFile, 'utf-8')
    if mswindows:
        fname = fname.encode('cp1252')
        oname = oname.encode('cp1252')

    acodec = select_audiocodec(False, inFile, tsn, 'video/mp4')
    settings = select_buffsize(tsn) + ['-c:v', 'copy'] + acodec
    if not acodec[1] == 'copy':
        settings += (select_audiobr(tsn) +
                     select_audiofr(inFile, tsn) +
                     select_audioch(inFile, tsn))
    settings += [select_audiolang(inFile, tsn),
                 select_ffmpegprams(tsn),
                 '-f', 'mp4']

    cmd = [ffmpeg_path, '-i', fname] + ' '.join(settings).split() + [oname]

    debug('transcoding to tivo model ' + tsn[:3] + ' using ffmpeg command:')
    debug(' '.join(cmd))

    ffmpeg = subprocess.Popen(cmd)
    debug('remuxing ' + inFile + ' to ' + outFile)
    if ffmpeg.wait():
        debug('error during remuxing')
        os.remove(outFile)
        return None

    return newname

def tivo_compatible(inFile, tsn='', mime=''):
    vInfo = video_info(inFile)

    message = (True, 'all compatible')
    if not config.get_bin('ffmpeg'):
        if mime not in ['video/x-tivo-mpeg', 'video/mpeg', '']:
            message = (False, 'no ffmpeg')
        return message

    while True:
        vmessage = tivo_compatible_video(vInfo, tsn, mime)
        if not vmessage[0]:
            message = vmessage
            break

        amessage = tivo_compatible_audio(vInfo, inFile, tsn, mime)
        if not amessage[0]:
            message = amessage
            break

        cmessage = tivo_compatible_container(vInfo, inFile, mime)
        if not cmessage[0]:
            message = cmessage

        break

    debug('TRANSCODE=%s, %s, %s' % (['YES', 'NO'][message[0]],
                                           message[1], inFile))
    return message

def video_info(inFile, cache=True):
    vInfo = dict()
    fname = unicode(inFile, 'utf-8')
    mtime = os.path.getmtime(fname)
    if cache:
        if inFile in info_cache and info_cache[inFile][0] == mtime:
            debug('CACHE HIT! %s' % inFile)
            return info_cache[inFile][1]

    vInfo['fname'] = inFile
    vInfo['Supported'] = True

    ffmpeg_path = config.get_bin('ffmpeg')
    if not ffmpeg_path:
        if os.path.splitext(inFile)[1].lower() not in ['.mpg', '.mpeg',
                                                       '.vob', '.tivo', '.ts']:
            vInfo['Supported'] = False
        vInfo.update({'millisecs': 0, 'vWidth': 704, 'vHeight': 480,
                      'rawmeta': {}})
        if cache:
            info_cache[inFile] = (mtime, vInfo)
        return vInfo

    if mswindows:
        fname = fname.encode('cp1252')
    cmd = [ffmpeg_path, '-i', fname]
    # Windows and other OS buffer 4096 and ffmpeg can output more than that.
    err_tmp = tempfile.TemporaryFile()
    ffmpeg = subprocess.Popen(cmd, stderr=err_tmp, stdout=subprocess.PIPE,
                              stdin=subprocess.PIPE)

    # wait configured # of seconds: if ffmpeg is not back give up
    limit = config.getFFmpegWait()
    if limit:
        for i in xrange(limit * 20):
            time.sleep(.05)
            if not ffmpeg.poll() == None:
                break

        if ffmpeg.poll() == None:
            kill(ffmpeg)
            vInfo['Supported'] = False
            if cache:
                info_cache[inFile] = (mtime, vInfo)
            return vInfo
    else:
        ffmpeg.wait()

    err_tmp.seek(0)
    output = err_tmp.read()
    err_tmp.close()
    debug('ffmpeg output=%s' % output)

    attrs = {'container': r'Input #0, ([^,]+),',
             'vCodec': r'Video: ([^, ]+)',             # video codec
             'aKbps': r'.*Audio: .+, (.+) (?:kb/s).*',     # audio bitrate
             'aCodec': r'.*Audio: ([^, ]+)',             # audio codec
             'aFreq': r'.*Audio: .+, (.+) (?:Hz).*',       # audio frequency
             'subType': r'.*Subtitle: ([a-zA-Z]+)',       # subtitle type
             'mapVideo': r'([0-9]+[.:]+[0-9]+).*: Video:.*'}  # video mapping

    for attr in attrs:
        rezre = re.compile(attrs[attr])
        x = rezre.search(output)
        if x:
            vInfo[attr] = x.group(1)
        else:
            if attr in ['container', 'vCodec']:
                vInfo[attr] = ''
                vInfo['Supported'] = False
            else:
                vInfo[attr] = None
            debug('failed at ' + attr)

    rezre = re.compile(r'.*Audio: .+, (?:(\d+)(?:(?:\.(\d).*)?(?: channels.*)?)|(stereo|mono)),.*')
    x = rezre.search(output)
    if x:
        if x.group(3):
            if x.group(3) == 'stereo':
                vInfo['aCh'] = 2
            elif x.group(3) == 'mono':
                vInfo['aCh'] = 1
        elif x.group(2):
            vInfo['aCh'] = int(x.group(1)) + int(x.group(2))
        elif x.group(1):
            vInfo['aCh'] = int(x.group(1))
        else:
            vInfo['aCh'] = None
            debug('failed at aCh')
    else:
        vInfo['aCh'] = None
        debug('failed at aCh')

    rezre = re.compile(r'.*Video: .+, (\d+)x(\d+)[, ].*')
    x = rezre.search(output)
    if x:
        vInfo['vWidth'] = int(x.group(1))
        vInfo['vHeight'] = int(x.group(2))
    else:
        vInfo['vWidth'] = ''
        vInfo['vHeight'] = ''
        vInfo['Supported'] = False
        debug('failed at vWidth/vHeight')

    rezre = re.compile(r'.*Video: .+, (.+) (?:fps|tb\(r\)|tbr).*')
    x = rezre.search(output)
    if x:
        vInfo['vFps'] = x.group(1)
        if '.' not in vInfo['vFps']:
            vInfo['vFps'] += '.00'

        # Allow override only if it is mpeg2 and frame rate was doubled
        # to 59.94

        if vInfo['vCodec'] == 'mpeg2video' and vInfo['vFps'] != '29.97':
            # First look for the build 7215 version
            rezre = re.compile(r'.*film source: 29.97.*')
            x = rezre.search(output.lower())
            if x:
                debug('film source: 29.97 setting vFps to 29.97')
                vInfo['vFps'] = '29.97'
            else:
                # for build 8047:
                rezre = re.compile(r'.*frame rate differs from container ' +
                                   r'frame rate: 29.97.*')
                debug('Bug in VideoReDo')
                x = rezre.search(output.lower())
                if x:
                    vInfo['vFps'] = '29.97'
    else:
        vInfo['vFps'] = ''
        vInfo['Supported'] = False
        debug('failed at vFps')

    durre = re.compile(r'.*Duration: ([0-9]+):([0-9]+):([0-9]+)\.([0-9]+),')
    d = durre.search(output)

    if d:
        vInfo['millisecs'] = ((int(d.group(1)) * 3600 +
                               int(d.group(2)) * 60 +
                               int(d.group(3))) * 1000 +
                              int(d.group(4)) * (10 ** (3 - len(d.group(4)))))
    else:
        vInfo['millisecs'] = 0

    # get bitrate of source for tivo compatibility test.
    rezre = re.compile(r'.*bitrate: (.+) (?:kb/s).*')
    x = rezre.search(output)
    if x:
        vInfo['kbps'] = x.group(1)
    else:
        # Fallback method of getting video bitrate
        # Sample line:  Stream #0.0[0x1e0]: Video: mpeg2video, yuv420p,
        #               720x480 [PAR 32:27 DAR 16:9], 9800 kb/s, 59.94 tb(r)
        rezre = re.compile(r'.*Stream #0\.0\[.*\]: Video: mpeg2video, ' +
                           r'\S+, \S+ \[.*\], (\d+) (?:kb/s).*')
        x = rezre.search(output)
        if x:
            vInfo['kbps'] = x.group(1)
        else:
            vInfo['kbps'] = None
            debug('failed at kbps')

    # get par.
    rezre = re.compile(r'.*Video: .+PAR ([0-9]+):([0-9]+) DAR [0-9:]+.*')
    x = rezre.search(output)
    if x and x.group(1) != "0" and x.group(2) != "0":
        vInfo['par1'] = x.group(1) + ':' + x.group(2)
        vInfo['par2'] = float(x.group(1)) / float(x.group(2))
    else:
        vInfo['par1'], vInfo['par2'] = None, None

    # get dar.
    rezre = re.compile(r'.*Video: .+DAR ([0-9]+):([0-9]+).*')
    x = rezre.search(output)
    if x and x.group(1) != "0" and x.group(2) != "0":
        vInfo['dar1'] = x.group(1) + ':' + x.group(2)
    else:
        vInfo['dar1'] = None

    # get Audio Stream mapping.
    rezre = re.compile(r'([0-9]+[.:]+[0-9]+)(.*): Audio:(.*)')
    x = rezre.search(output)
    amap = []
    if x:
        for x in rezre.finditer(output):
            amap.append((x.group(1), x.group(2) + x.group(3)))
    else:
        amap.append(('', ''))
        debug('failed at mapAudio')
    vInfo['mapAudio'] = amap

    vInfo['par'] = None

    # get Metadata dump (newer ffmpeg).
    lines = output.split('\n')
    rawmeta = {}
    flag = False

    for line in lines:
        if line.startswith('  Metadata:'):
            flag = True
        else:
            if flag:
                if line.startswith('  Duration:'):
                    flag = False
                else:
                    try:
                        key, value = [x.strip() for x in line.split(':', 1)]
                        try:
                            value = value.decode('utf-8')
                        except:
                            if sys.platform == 'darwin':
                                value = value.decode('macroman')
                            else:
                                value = value.decode('cp1252')
                        rawmeta[key] = [value]
                    except:
                        pass

    vInfo['rawmeta'] = rawmeta

    data = metadata.from_text(inFile)
    for key in data:
        if key.startswith('Override_'):
            vInfo['Supported'] = True
            if key.startswith('Override_mapAudio'):
                audiomap = dict(vInfo['mapAudio'])
                newmap = shlex.split(data[key])
                audiomap.update(zip(newmap[::2], newmap[1::2]))
                vInfo['mapAudio'] = sorted(audiomap.items(),
                                           key=lambda (k,v): (k,v))
            elif key.startswith('Override_millisecs'):
                vInfo[key.replace('Override_', '')] = int(data[key])
            else:
                vInfo[key.replace('Override_', '')] = data[key]
        #legacy subtitle support to be removed
        elif key.lower() == 'subtitles':
            vInfo['subtitles'] = data[key]
        elif key.lower() == 'subfile':
            vInfo['subFile'] = data[key]

    if cache:
        info_cache[inFile] = (mtime, vInfo)
    debug("; ".join(["%s=%s" % (k, v) for k, v in vInfo.items()]))
    return vInfo

def audio_check(inFile, tsn):
    cmd_string = ('-y -c:v mpeg2video -r 29.97 -b:v 1000k -c:a copy ' +
                  select_audiolang(inFile, tsn) + ' -t 00:00:01 -f vob -')
    fname = unicode(inFile, 'utf-8')
    if mswindows:
        fname = fname.encode('cp1252')
    cmd = [config.get_bin('ffmpeg'), '-i', fname] + cmd_string.split()
    ffmpeg = subprocess.Popen(cmd, stdout=subprocess.PIPE)
    fd, testname = tempfile.mkstemp()
    testfile = os.fdopen(fd, 'wb')
    try:
        shutil.copyfileobj(ffmpeg.stdout, testfile)
    except:
        kill(ffmpeg)
        testfile.close()
        vInfo = None
    else:
        testfile.close()
        vInfo = video_info(testname, False)
    os.remove(testname)
    return vInfo

def supported_format(inFile):
    if video_info(inFile)['Supported']:
        return True
    else:
        debug('FALSE, file not supported %s' % inFile)
        return False

def kill(popen):
    debug('killing pid=%s' % str(popen.pid))
    if mswindows:
        win32kill(popen.pid)
    else:
        import os, signal
        for i in xrange(3):
            debug('sending SIGTERM to pid: %s' % popen.pid)
            os.kill(popen.pid, signal.SIGTERM)
            time.sleep(.5)
            if popen.poll() is not None:
                debug('process %s has exited' % popen.pid)
                break
        else:
            while popen.poll() is None:
                debug('sending SIGKILL to pid: %s' % popen.pid)
                os.kill(popen.pid, signal.SIGKILL)
                time.sleep(.5)

def win32kill(pid):
    import ctypes
    handle = ctypes.windll.kernel32.OpenProcess(1, False, pid)
    ctypes.windll.kernel32.TerminateProcess(handle, -1)
    ctypes.windll.kernel32.CloseHandle(handle)

def gcd(a, b):
    while b:
        a, b = b, a % b
    return a
