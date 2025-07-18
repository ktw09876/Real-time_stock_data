import os
import sys
import logging
from datetime import datetime, timedelta, timezone
from pymongo import MongoClient

"""
필수 환경 변수가 설정되었는지 확인
"""
def test_env_var():
    test_env_var = [
        'MONGO_HOST',
        'MONGO_PORT',
        'MONGO_DATABASE',
        'KAFKA_TOPICS',
    ]

    missing_vars = []
    for var in test_env_var:
        if not os.getenv(var):
            missing_vars.append(var)
    
    if missing_vars:
        raise ValueError(f"필수 환경 변수가 설정되지 않았습니다: {', '.join(missing_vars)}")
    
"""
MongoDB의 최신 데이터 지연 시간을 확인하고,
정상이면 종료 코드 0, 비정상이면 1을 반환하는 함수.
"""
def check_latency():

    # 0. 환경 변수 확인
    test_env_var()

    # --- 환경 변수 로드 ---
    MONGO_HOST = os.getenv('MONGO_HOST')
    MONGO_PORT_STR = os.getenv('MONGO_PORT')
    MONGO_DB_NAME = os.getenv('MONGO_DATABASE')
    COLLECTION_NAME = os.getenv('KAFKA_TOPICS')        
    MONGO_PORT = int(MONGO_PORT_STR)

    # 로거 설정
    logging.basicConfig(level=logging.INFO,
                        format='%(asctime)s - %(levelname)s - %(message)s',
                        stream=sys.stdout)

    client = None
    try:
        logging.info(f"MongoDB에 연결 시도: {MONGO_HOST}:{MONGO_PORT}")
        client = MongoClient(host=MONGO_HOST, port=MONGO_PORT, serverSelectionTimeoutMS=5000)
        client.admin.command('ping')
        logging.info("MongoDB 연결에 성공했습니다.")
        
        db = client[MONGO_DB_NAME]
        collection = db[COLLECTION_NAME]
        
        KST = timezone(timedelta(hours=9))
        today_start_kst = datetime.now(KST).replace(hour=0, minute=0, second=0, microsecond=0)
        today_start_utc = today_start_kst.astimezone(timezone.utc)

        latest_doc = collection.find_one(
            {'insert_time': {'$gte': today_start_utc}},
            sort=[('insert_time', -1)]
        )
        
        if not latest_doc:
            logging.warning(f"오늘({today_start_kst.strftime('%Y-%m-%d')})의 데이터가 아직 수집되지 않았습니다.")
            sys.exit(1)

        # 1. DB에서 가져온 시간을 UTC 시간대로 변환
        last_insert_time_utc = latest_doc.get('insert_time').replace(tzinfo=timezone.utc)
        
        # 2. 현재 시간을 UTC로 가져옴
        current_time_utc = datetime.now(timezone.utc)

        # 3. 로그 출력을 위해 두 시간 모두 KST로 변환
        last_insert_time_kst = last_insert_time_utc.astimezone(KST)
        current_time_kst = current_time_utc.astimezone(KST)
        
        # 4. 시간 차이는 UTC 기준으로 계산
        time_diff_seconds = (current_time_utc - last_insert_time_utc).total_seconds()
        
        # 5. KST 기준으로 변환된 시간을 로그에 출력
        logging.info(f"마지막 데이터 저장 시간 (KST): {last_insert_time_kst.strftime('%Y-%m-%d %H:%M:%S')}")
        logging.info(f"현재 시간 (KST): {current_time_kst.strftime('%Y-%m-%d %H:%M:%S')}")
        logging.info(f"시간 차이: {time_diff_seconds:.2f} 초")

        latency_threshold = 300 # 5분

        if time_diff_seconds < latency_threshold:
            logging.info(f"데이터 수집이 정상입니다 (지연 시간 < {latency_threshold}초).")
            sys.exit(0) # 성공 시 종료 코드 0
        else:
            logging.error(f"경고: 데이터 수집이 {latency_threshold}초 이상 지연되었습니다.")
            sys.exit(1) # 실패 시 종료 코드 1

    except Exception as e:
        logging.error(f"오류 발생: {e}")
        sys.exit(1) # 에러 발생 시 종료 코드 1
    finally:
        if client:
            client.close()

if __name__ == "__main__":
    check_latency()