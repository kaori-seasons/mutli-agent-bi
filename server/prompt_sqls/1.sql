SELECT factory_name, SUM(car_model_count) 
FROM nitto_order
WHERE deliver_city='{0}'