/**
 * Copyright (C) 2012 LinkedIn Inc <opensource@linkedin.com>
 *
 * Licensed under the Apache License, Version 2.0 (the "License");
 * you may not use this file except in compliance with the License.
 * You may obtain a copy of the License at
 *
 *         http://www.apache.org/licenses/LICENSE-2.0
 *
 * Unless required by applicable law or agreed to in writing, software
 * distributed under the License is distributed on an "AS IS" BASIS,
 * WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
 * See the License for the specific language governing permissions and
 * limitations under the License.
 */
package com.linkedin.helix.controller.stages;

import java.util.ArrayList;
import java.util.Collections;
import java.util.HashMap;
import java.util.List;
import java.util.Map;

import com.linkedin.helix.model.Message;
import com.linkedin.helix.model.Partition;

public class MessageGenerationOutput
{

  private final Map<String, Map<Partition, List<Message>>> _messagesMap;

  public MessageGenerationOutput()
  {
    _messagesMap = new HashMap<String, Map<Partition, List<Message>>>();

  }

  public void addMessage(String resourceName, Partition resource,
      Message message)
  {
    if (!_messagesMap.containsKey(resourceName))
    {
      _messagesMap.put(resourceName,
          new HashMap<Partition, List<Message>>());
    }
    if (!_messagesMap.get(resourceName).containsKey(resource))
    {
      _messagesMap.get(resourceName).put(resource,
          new ArrayList<Message>());

    }
    _messagesMap.get(resourceName).get(resource).add(message);

  }

  public List<Message> getMessages(String resourceName,
      Partition resource)
  {
    Map<Partition, List<Message>> map = _messagesMap.get(resourceName);
    if (map != null)
    {
      return map.get(resource);
    }
    return Collections.emptyList();

  }
  
  @Override
  public String toString()
  {
    return _messagesMap.toString();
  }
}
