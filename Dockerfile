FROM python:3.10-slim

ENV SHIFT_ARGS='--redeem bl3:steam --schedule' \
    TZ='America/Chicago'

COPY . /autoshift/
RUN pip install -r ./autoshift/requirements.txt && \
    mkdir -p ./autoshift/data
CMD python ./autoshift/auto.py --user ${SHIFT_USER} --pass ${SHIFT_PASS} ${SHIFT_ARGS}
