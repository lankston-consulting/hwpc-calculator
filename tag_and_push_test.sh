#!/bin/sh
# Helper function to push the 2 required images for hwpc-calc to AWS
echo Tag and Push hwpc-calc
docker tag hwpc-calc:client 234659567514.dkr.ecr.us-west-2.amazonaws.com/hwpc-calc:client
docker push 234659567514.dkr.ecr.us-west-2.amazonaws.com/hwpc-calc:client
docker tag hwpc-calc:worker 234659567514.dkr.ecr.us-west-2.amazonaws.com/hwpc-calc:worker
docker push 234659567514.dkr.ecr.us-west-2.amazonaws.com/hwpc-calc:worker

