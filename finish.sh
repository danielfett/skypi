#!/bin/bash

MODE=$1
FILESLIST=$2
TIMELAPSE=$3
OVERLAY=$4

if [ "$MODE" == "day" ]
then
    COMPOSEMODE=darken
else    
    COMPOSEMODE=lighten
fi

mencoder -nosound -ovc lavc -lavcopts vcodec=mpeg4:aspect=4/3:vbitrate=8000000 -vf scale=1640:1232 -o "$TIMELAPSE" -mf type=jpeg:fps=20 mf://@"$FILESLIST"

FIRST=1
while read F; do
    if [ $FIRST == 1 ]; then
        cp "$F" "$OVERLAY"
        FIRST=0
    else
        convert "$OVERLAY" $F -gravity center -compose $COMPOSEMODE -composite -format jpg "$OVERLAY"
    fi
done < "$FILESLIST"

