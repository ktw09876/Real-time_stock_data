# -*- coding: utf-8 -*-
import os
from datetime import datetime, timezone, timedelta
from pyspark.sql import SparkSession
from pyspark.sql.functions import from_json, col, date_format, lit, round as spark_round, when
from pyspark.sql.types import StructType, StructField, StringType, DoubleType, LongType
import s3fs

def main():
    """
    [최종] Kafka의 체결가 토픽(H0STCNT0) 하나만 구독하여,
    VWAP, 체결강도, 매수/매도 압력 등 종합적인 시장 지표를 계산하고
    그 결과를 S3의 단일 CSV 파일에 추가하는 Spark Structured Streaming 애플리케이션.
    """
    # 1. 환경 변수에서 설정 값 로드 및 검증
    required_vars = [
        'KAFKA_BROKER_INTERNAL', 'SPARK_TRADE_TOPIC', 'S3_BUCKET'
    ]
    missing_vars = [var for var in required_vars if not os.getenv(var)]
    if missing_vars:
        raise ValueError(f"필수 환경 변수가 설정되지 않았습니다: {', '.join(missing_vars)}")

    KAFKA_BROKER = os.getenv('KAFKA_BROKER_INTERNAL')
    # .env 파일과 변수 이름을 맞춥니다.
    TRADE_TOPIC = os.getenv('SPARK_TRADE_TOPIC')
    S3_BUCKET = os.getenv('S3_BUCKET')
    
    KST = timezone(timedelta(hours=9))

    # 2. Spark Session 생성
    spark = (SparkSession.builder
             .appName("RealtimeMarketAnalysisStateless")
             .getOrCreate())
    
    spark.sparkContext._jsc.hadoopConfiguration().set("fs.s3a.impl", "org.apache.hadoop.fs.s3a.S3AFileSystem")

    # 3. 데이터 스키마 정의 (H0STCNT0 JSON 파싱용)
    trade_schema = StructType([
        StructField("stck_prpr", StringType()),
        StructField("acml_vol", StringType()),
        StructField("acml_tr_pbmn", StringType()),
        StructField("cttr", StringType()),
        StructField("total_askp_rsqn", StringType()),
        StructField("total_bidp_rsqn", StringType()),
    ])

    # 4. Kafka 스트림 읽기 (이제 토픽 하나만 구독합니다)
    kafka_stream = (spark.readStream
            .format("kafka")
            .option("kafka.bootstrap.servers", KAFKA_BROKER)
            .option("subscribe", TRADE_TOPIC)
            .option("failOnDataLoss", "false")
            .load())

    # 5. 최종 지표 계산 (Stateless 변환)
    # JSON 메시지를 파싱하여 데이터프레임으로 변환
    parsed_df = kafka_stream.select(
        # col("value").cast("string"), # 원본
        from_json(col("value").cast("string"), trade_schema).alias("data"), 
        col("key").cast("string").alias("stock_code")
    )

    # 지표를 계산하고 컬럼 이름을 부여합니다.
    result_df = (parsed_df
        .select(
            col("stock_code"),
            col("data.acml_vol").alias("cumulative_volume"), #cumulative_volume
            col("data.acml_tr_pbmn").alias("cumulative_value"),
            col("data.cttr").alias("trade_strength"),
            col("data.total_askp_rsqn").alias("total_ask_qty"),
            col("data.total_bidp_rsqn").alias("total_bid_qty"),
        ).filter(col("cumulative_volume") > 0) # 0으로 나누는 오류를 방지하기 위해 필터링
        .withColumn("vwap", col("cumulative_value") / col("cumulative_volume")) # VWAP (거래량 가중 평균 가격)
        .withColumn("buy_sell_pressure", when(col("total_ask_qty") > 0, col("total_bid_qty") / col("total_ask_qty")).otherwise(0)) # 매수/매도 압력: total_ask_qty가 0보다 클 때만 계산하고, 아니면 0을 반환
    )

    # 6. S3에 스트림 쓰기
    today_str = datetime.now(KST).strftime("%Y-%m-%d")
    def write_to_s3(batch_df, batch_id):
        print(f"--- [ Batch ID: {batch_id} ] --- write_to_s3 함수 진입 ---")
        # 디버깅
        batch_df.cache()
        print("Batch DataFrame 스키마:")
        batch_df.printSchema()
        print("Batch DataFrame 내용 (상위 5개):")
        batch_df.show(5, truncate=False)
        
        print(f"--- [ Batch ID: {batch_id} ] --- S3에 최종 리포트 추가 시작...")        
        processing_time = datetime.now(KST).strftime("%Y-%m-%d %H:%M:%S")
        df_to_write = (batch_df
                       .withColumn("update_time", lit(processing_time))
                       .withColumn("vwap", spark_round(col("vwap"), 2))
                       .withColumn("buy_sell_pressure", spark_round(col("buy_sell_pressure"), 2))
        )
        
        pandas_df = df_to_write.toPandas()
        s3 = s3fs.S3FileSystem()
        
        for stock_code_val, group_df in pandas_df.groupby('stock_code'):
            target_file_path = f"s3a://{S3_BUCKET}/daily_report/stock_code={stock_code_val}/{today_str}.csv"
            final_columns = ['stock_code', 'update_time', 'cumulative_volume', 'cumulative_value', 'vwap', 'trade_strength', 'buy_sell_pressure']
            data_to_write = group_df[final_columns]
            
            try:
                file_exists = s3.exists(target_file_path)
                csv_buffer = data_to_write.to_csv(header=not file_exists, index=False)
                with s3.open(target_file_path, 'a') as f: f.write(csv_buffer)
            except Exception as e: print(f"Error for {stock_code_val}: {e}")

        batch_df.unpersist() # 캐시 해제
        print(f"--- [ Batch ID: {batch_id} ] --- 쓰기 완료.")

    query = (result_df.writeStream
             .outputMode("update")
             .foreachBatch(write_to_s3)
             .trigger(processingTime="30 seconds") # 성능 경고를 피하기 위해 주기를 30초로 조정
             .option("checkpointLocation", f"s3a://{S3_BUCKET}/checkpoints/{today_str}")
             .start()
    )

    print(f"스트리밍 쿼리 시작. 오늘({today_str})의 종합 시장 분석 리포트를 추가합니다.")
    query.awaitTermination()

if __name__ == "__main__":
    main()
