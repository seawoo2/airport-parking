SELECT batch_id,
       target_hour,
       terminal,
       direction,
       expected_passengers
FROM public.passenger_forecasts
LIMIT 1000;