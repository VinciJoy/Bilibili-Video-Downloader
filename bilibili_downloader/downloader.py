import re
import os
import sys
import requests
from utils import extract_json, merge_flv

from collections import defaultdict
from contextlib import closing
from multiprocessing import cpu_count
from concurrent.futures import ThreadPoolExecutor, as_completed


class BiliDownloader:
    USER_AGENT = 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/79.0.3945.88 Safari/537.36'

    def __init__(self, url, dir_name, cookie, max_workers=None):
        self.url = url
        self.DIRNAME = dir_name
        self.COOKIE = cookie
        if max_workers is None:
            max_workers = cpu_count() // 2
        self.max_workers = max_workers

        self.qn2desc = {
            16: '360P',
            32: '480P',
            64: '720P',
            74: '720P60',
            80: '1080P',
            112: '1080P+',
            116: '1080P60'
        }
        self._keys = {
            'video': ['title', 'desc', 'pic'],
            'bangumi': ['title', 'series', 'evaluate', 'cover']
        }
        self.get_basic_info()

    def retry(self, download_addres, stream, headers, timeout):
        count = 0
        while True:
            try:
                f = requests.get(download_addres, stream=stream, headers=headers, timeout=timeout)
                return f
            except:
                count += 1
                print("timeout! retry {} times!".format(count))
                continue

    def get_basic_info(self):
        headers = {'User-Agent': self.USER_AGENT}
        res = requests.get(self.url, headers=headers)
        self.html = extract_json(res.text.split('__INITIAL_STATE__=')[1])

        if 'videoData' in self.html.keys():
            self.type = 'video'
            basic_info = self.html['videoData']
        elif 'epList' in self.html.keys():
            self.type = 'bangumi'
            basic_info = self.html['mediaInfo']
        else:
            print('Invalid URL!')
            sys.exit(0)

        self.basic_info = {k: basic_info[k] for k in self._keys[self.type]}

    def get_info(self, page):
        if self.type == 'video':
            video_list = self.html['videoData']['pages']
            aid = self.html['videoData']['aid']
        elif self.type == 'bangumi':
            video_list = self.html['epList']

        if isinstance(page, int):
            page = [page]
        if page[0] == 0:
            page = [i + 1 for i in range(len(video_list))]
        assert max(page) <= len(video_list), 'no such page, check the maximum page'
        assert min(page) > 0, 'no such page, check the minimum page'

        info = defaultdict(list)
        info['page'] = page

        for i in page:
            v_l = video_list[i - 1]

            if self.type == 'video':
                info['aid'].append(aid)
                info['title'].append(v_l['part'])
            elif self.type == 'bangumi':
                info['aid'].append(v_l['aid'])
                info['title'].append(v_l['titleFormat'] + ' ' + v_l['longTitle'])
            info['cid'].append(v_l['cid'])

        return info

    def add_play_url(self, info, quality):
        assert quality > 0, 'quality must > 0'

        length = len(info['page'])
        info['play_url'] = [[] for _ in range(length)]

        headers = {
            'User-Agent': self.USER_AGENT,
            'Cookie': self.COOKIE
        }
        for i in range(length):
            cid = info['cid'][i]
            aid = info['aid'][i]
            url = 'https://api.bilibili.com/x/player/playurl?cid={}&avid={}'.format(cid, aid)
            html = requests.get(url, headers=headers).json()
            data = html['data']
            if data is None:
                print('\nInvalid Cookie! You need use or update the VIP COOKIE!')
                sys.exit(1)

            actual_gear = min(quality, len(data['accept_quality']))
            actual_quality = data['accept_quality'][-actual_gear]
            info['actual_quality'].append(actual_quality)

            url = 'https://api.bilibili.com/x/player/playurl?cid={}&avid={}&qn={}'.format(cid, aid, actual_quality)
            html = requests.get(url, headers=headers).json()
            data = html['data']

            for du in data['durl']:
                info['play_url'][i].append(du['url'])

    def download_single(self, file_path, play_url):
        file_dir, file_name = os.path.split(file_path)
        done_file_name = file_name[2:]
        done_file_path = os.path.join(file_dir, done_file_name)
        block_size = 1024 * 1024

        if not os.path.exists(done_file_path):
            if os.path.exists(file_path):
                downloaded_size = os.path.getsize(file_path)
                downloaded_index = int(downloaded_size / block_size)
            else:
                downloaded_index = 0
            headers = {
                'User-Agent': self.USER_AGENT,
                'Referer': self.url,
            }
            res = requests.get(play_url, headers=headers, stream=True)
            length = int(res.headers['Content-Length'])
            blocks = int(length / block_size)
            the_last_blocks = length % block_size
            while downloaded_index < blocks:
                with open(file_path, 'ab') as f:
                    headers["Range"] = "bytes={}-{}".format(downloaded_index * block_size,
                                                            (downloaded_index + 1) * block_size)
                    block = self.retry(play_url, headers=headers, stream=True, timeout=20)
                    f.write(block.content)
                    print('\r    {}/{} downloading'.format(downloaded_index, blocks), end='')
                    downloaded_index += 1
            if the_last_blocks:
                with open(file_path, 'ab') as f:
                    headers["Range"] = "bytes={}-{}".format(blocks * block_size, blocks * block_size + the_last_blocks)
                    block = self.retry(play_url, headers=headers, stream=True, timeout=20)
                    f.write(block.content)
                    print("the last block wrote, downloading success!")
            os.rename(file_path, done_file_path)

        print('\n    Done: {}\n'.format(done_file_path))
        return done_file_path

    def postprocess(self, video_dir, file_path):
        file_name = os.path.split(file_path)[1]
        depart = re.search(r'P(\d+).(\d+)-(\d+).(.+).flv', file_name)
        if depart is not None:
            page = depart.group(1)
            part = depart.group(2)
            length = depart.group(3)
            titlename = depart.group(4)

            if page not in self.container.keys():
                target = os.path.join(video_dir, '{}.flv'.format(titlename))
                self.container[page] = [target, file_path]
            else:
                self.container[page].append(file_path)

                if len(self.container[page]) == int(length) + 1:
                    flvs = sorted(self.container[page][1:])
                    merge_flv(flvs, self.container[page][0])  # merge_flv (list of merge flvs, target file)
                    for f in self.container[page][1:]:
                        os.remove(f)
                    print('\n    Merged successfully:', self.container[page][0])

    def __call__(self, page=1, quality=3, mode='common', info=None):
        assert mode in ['common', 'info', 'download']
        if mode == 'download':
            assert info is not None, "info should not be None while mode='download'"
        else:
            info = self.get_info(page)
            self.add_play_url(info, quality)
            if mode == 'info':
                return dict(info)

        title = self.basic_info['title']
        sub_title = re.sub('[\/:*?"<>|]', ' ', title)
        qn_desc = self.qn2desc[info['actual_quality'][0]]
        midname = '{}/{}--{}'.format(self.type, sub_title, qn_desc)

        print('Downloading {}({}):'.format(self.type, qn_desc))
        print('{}----{}'.format(title, info['page']))

        video_dir = os.path.join(self.DIRNAME, midname)
        if not os.path.exists(video_dir):
            os.makedirs(video_dir)

        executor = ThreadPoolExecutor(max_workers=self.max_workers)
        tasks = []
        for i, p in enumerate(info['page']):
            sub_title = re.sub('[\/:*?"<>|]', ' ', info['title'][i])
            len_play_url = len(info['play_url'][i])

            for j in range(len_play_url):
                play_url = info['play_url'][i][j]

                if len_play_url == 1:
                    file_path = os.path.join(video_dir, '0.{}.flv'.format(sub_title))
                else:
                    # e.g. https://www.bilibili.com/bangumi/play/ss2539/
                    file_path = os.path.join(video_dir,
                                             '0.P{}.{}-{}.{}.flv'.format(p, j + 1, len_play_url, sub_title))
                tasks.append(executor.submit(self.download_single, file_path, play_url))

        self.container = {}
        for future in as_completed(tasks):
            file_path = future.result()
            self.postprocess(video_dir, file_path)
        print('\nAll done!')
