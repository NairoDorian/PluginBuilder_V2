/* Shared Use License: This file is owned by Derivative Inc. (Derivative)
* and can only be used, and/or modified for use, in conjunction with
* Derivative's TouchDesigner software, and only if you are a licensee who has
* accepted Derivative's TouchDesigner license or assignment agreement
* (which also govern the use of this file). You may share or redistribute
* a modified version of this file provided the following conditions are met:
*
* 1. The shared file or redistribution must retain the information set out
* above and this list of conditions.
* 2. Derivative's name (Derivative Inc.) or its trademarks may not be used
* to endorse or promote products derived from this file without specific
* prior written permission from Derivative.
*/

#include "FrameQueue.h"
#include <assert.h>

using namespace TD;

// Maximum number of completed frames waiting for the main thread.
static const size_t MaxQueueSize = 2;

FrameQueue::FrameQueue(TOP_Context* context) :
	myContext(context)
{

}

FrameQueue::~FrameQueue()
{
	while (!myUpdatedBuffers.empty())
	{
		myUpdatedBuffers.pop_front();
	}
}

// Producer side. Reuses the oldest queued (never consumed) buffer when the
// queue is full, otherwise allocates a new one.
OP_SmartRef<TOP_Buffer>
FrameQueue::getBufferToUpdate(uint64_t byteSize, TOP_BufferFlags flags)
{
	OP_SmartRef<TOP_Buffer> buf;

	{
		std::lock_guard<std::mutex> lock(myLock);

		// If we've already reached the max queue size, replace the oldest buffer
		// instead of requesting a new one and growing the queue even more.
		if (myUpdatedBuffers.size() >= MaxQueueSize)
		{
			buf = std::move(myUpdatedBuffers.front().buf);
			myUpdatedBuffers.pop_front();
		}
	}

	// If the size of this buffer is way off or if the flags are wrong,
	// don't use it.
	if (buf && (buf->size < byteSize || buf->size > byteSize * 2 || buf->flags != flags))
	{
		buf.release();
	}

	// If we don't have a buffer yet, allocate one. createOutputBuffer() is
	// thread-safe, so it is called without holding the queue lock (the official
	// sample held it here, blocking the main thread during the allocation).
	if (!buf)
		buf = myContext->createOutputBuffer(byteSize, flags, nullptr);

	return buf;
}

void
FrameQueue::updateComplete(const BufferInfo& bufInfo)
{
	assert(bufInfo.buf);
	std::lock_guard<std::mutex> lock(myLock);
	myUpdatedBuffers.push_back(bufInfo);
}

void
FrameQueue::updateCancelled(OP_SmartRef<TOP_Buffer>* buf)
{
	myContext->returnBuffer(buf);
}

// Consumer side (main thread). The official sample returned the oldest frame,
// which added a frame of latency whenever the producer was ahead.
BufferInfo
FrameQueue::getBufferToUpload()
{
	BufferInfo buf;
	OP_SmartRef<TOP_Buffer> dropped[MaxQueueSize];
	size_t numDropped = 0;

	{
		std::lock_guard<std::mutex> lock(myLock);

		if (!myUpdatedBuffers.empty())
		{
			buf = std::move(myUpdatedBuffers.back());
			myUpdatedBuffers.pop_back();
		}

		// Everything older than the newest frame is out of date.
		while (!myUpdatedBuffers.empty())
		{
			if (numDropped < MaxQueueSize)
				dropped[numDropped++] = std::move(myUpdatedBuffers.front().buf);
			myUpdatedBuffers.pop_front();
		}
	}

	// Hand the dropped buffers back so a later createOutputBuffer() can reuse
	// them, outside the lock.
	for (size_t i = 0; i < numDropped; i++)
		myContext->returnBuffer(&dropped[i]);

	return buf;
}
