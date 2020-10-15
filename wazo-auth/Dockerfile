FROM wazoplatform/wazo-auth

RUN apt-get update
RUN apt-get install -y jq curl

ADD ./entry.sh /entry.sh

ENTRYPOINT /entry.sh