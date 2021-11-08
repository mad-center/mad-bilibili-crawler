import math
import time
import pymongo
import requests

from datetime import datetime
from fake_useragent import UserAgent
from pymongo import UpdateOne

# url config
# reference: https://github.com/SocialSisterYi/bilibili-API-collect/blob/master/video/video_zone.md
primary_partition_name = '番剧(主分区)'
primary_partition_code = 'anime'
primary_partition_tid = 13

subpartition_name = '连载动画'
subpartition_code = 'serial'
subpartition_tid = 33

path = 'https://api.bilibili.com/x/web-interface/newlist'
type = 0
rid = subpartition_tid
ps = 50

# requests config
referer = f'https://www.bilibili.com/v/{primary_partition_code}/{subpartition_code}'

# db config
db_name = 'bilibili'
conn_str = f"mongodb://127.0.0.1:27017/{db_name}"
client = pymongo.MongoClient(conn_str, serverSelectionTimeoutMS=5000)

db = client[db_name]
current_collection = db[subpartition_code]
crawl_progress_collection = db['crawl_progress']


def total_video_count():
    url = page_url(path, rid, type, pn=1, ps=50)
    data = fetch_page(url)
    if data:
        return int(data['page']['count'])
    else:
        return None


def random_useragent():
    ua = UserAgent()
    return ua.random


def request_headers(referer='', random_ua=True):
    """
      accept: */*
      accept-encoding: gzip, deflate, br
      accept-language: en,zh-CN;q=0.9,zh;q=0.8
      cookie: ...(省略)
      referer: https://www.bilibili.com/v/douga/mad/
      sec-ch-ua: "Google Chrome";v="95", "Chromium";v="95", ";Not A Brand";v="99"
      sec-ch-ua-mobile: ?0
      sec-ch-ua-platform: "Windows"
      sec-fetch-dest: script
      sec-fetch-mode: no-cors
      sec-fetch-site: same-site
      user-agent: Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/95.0.46
    """
    ua = 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/95.0.46'
    headers = {
        'accept': '*/*',
        'accept-encoding': 'gzip, deflate, br',
        'accept-language': 'en,zh-CN;q=0.9,zh;q=0.8',
        'referer': referer,
        'user-agent': random_useragent() if random_ua else ua
    }
    return headers


def upsert_video(data):
    if data:
        archives = data['archives']
        if len(archives) > 0:
            operations = []

            for idx, archive in enumerate(archives):
                filter = {'aid': archive['aid']}
                update = {'$set': archive}
                operations.append(UpdateOne(filter, update, upsert=True))

            try:
                result = current_collection.bulk_write(operations)
                print('insert: {0}, delete: {1}, modify: {2}'.format(result.inserted_count,
                                                                     result.deleted_count,
                                                                     result.modified_count))
                return True
            except Exception as e:
                print('ERROR: bulk_write ', e)
    else:
        print('upsert_video() data is None.')

    return False


def fetch_page(url, retry_max=3, timeout=10):
    retry_count = 0
    retry_max = retry_max

    while retry_count <= retry_max:
        try:
            headers = request_headers(referer)
            r = requests.get(url, headers, timeout=timeout)
            json = r.json()
            if json and json['code'] == 0:
                return json['data']
            else:
                print('response ok but no data')
                return None
        except Exception as e:
            print('ERROR: fetch_page', e)
            time.sleep(3)

            retry_count += 1
            print('fetch_page retry count: ', retry_count)

    return None


def page_url(path, rid, type, pn=1, ps=50):
    pn = pn
    ps = ps
    url = f'{path}?rid={rid}&type={type}&pn={pn}&ps={ps}'
    return url


def crawl_progress_page():
    """
    1. 数据库访问失败。返回None。
    2. 数据库访问成功，但是没有查询到doc，代表还没有初始化。直接返回0。
    3. 数据库访问成功，查询到doc，返回doc中的page字段。
    Returns
    -------
    """
    try:
        filter = {
            'partition_id': rid
        }
        doc = crawl_progress_collection.find_one(filter=filter)
        if doc:
            return doc['done_page']
        else:
            # 0 is initial value for done_page field
            return 0
    except Exception as e:
        print('ERROR: get next_page failed. MUST stop.')
        return None


def upsert_crawl_progress_page():
    try:
        # find_one_and_update() Returns ``None`` if no document matches the filter.
        res = crawl_progress_collection.find_one_and_update(
            {
                'partition_id': rid
            },
            {
                '$setOnInsert': {
                    'partition_id': rid,
                    'create_time': datetime.now(),
                    'partition_name': subpartition_name,
                    'partition_code': subpartition_code
                },
                '$inc': {'done_page': 1},
                '$set': {'update_time': datetime.now()}
            },
            upsert=True
        )
        return True
    except Exception as e:
        print('ERROR: update page failed', e)

    return False


def crawl():
    done_page = crawl_progress_page()
    total_count = total_video_count()
    page_count = math.ceil(total_count / ps)
    print(f'Analyze: done_page={done_page}, page_count={page_count}, current_count={total_count}')

    if done_page >= page_count:
        return

    next_page = done_page + 1
    crawl_by_range(next_page, page_count, ps)

    print('stop crawler.')


def crawl_by_range(page_start, page_end, ps, patch=False):
    for i in range(page_start, page_end + 1):

        url = page_url(path, rid, type, pn=i, ps=ps)
        print('url=', url)

        data = fetch_page(url)
        should_break = False

        if data and (len(data['archives']) > 0):
            print('count={0}, num={1}, size={2}, archives len={3}'
                  .format(data['page']['count'], data['page']['num'], data['page']['size'], len(data["archives"])))

            # do better: the all below ops should be wrapped by transaction
            if not patch:
                db_result = upsert_video(data) and upsert_crawl_progress_page()
                should_break = not db_result
            else:
                db_result = upsert_video(data)
                should_break = not db_result
        else:
            should_break = True
            print('ERROR: fetch page failed or data[archives] is empty.')

        if should_break:
            print('break in crawler: db OPS error or no data. cur page={0}'.format(i))
            break

        print('DONE: crawl page {0} and update page to {0}'.format(i, i))

        time.sleep(2)


if __name__ == '__main__':
    crawl()
    # crawl_by_range(1, 20, 50, patch=True)
